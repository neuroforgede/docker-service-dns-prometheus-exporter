#!/usr/bin/python3
import docker
from typing import Dict, List, TypeVar, Callable, Any
from functools import reduce
from collections import defaultdict
import json
import dns.resolver
import os
import traceback
import subprocess
import platform
import datetime
import signal
from datetime import datetime, timedelta
from time import sleep
from prometheus_client import start_http_server, Gauge

from threading import Event
import signal

from contract import *

T = TypeVar('T')
K = TypeVar('K')

def group_by(key: Callable[[T], K], seq: List[T]) -> Dict[K, List[T]]:
  return reduce(lambda grp, val: grp[key(val)].append(val) or grp, seq, defaultdict(list))

def dump_as_json_string(input: Dict[str, List[ServiceEndpoint]]) -> Dict[str, List[Any]]:
  as_dict = {k: [elem.to_dict() for elem in v] for k,v in input.items()}
  return json.dumps(as_dict, indent=4)

exit_event = Event()

shutdown: bool = False
def handle_shutdown(signal: Any, frame: Any) -> None:
    print_timed(f"received signal {signal}. shutting down...")
    exit_event.set()

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


APP_NAME = "Docker DNS Check Exporter"
DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS = Gauge('docker_service_dns_resolution_success',
  'Docker Service DNS Resolution Success',
  [
    'docker_hostname',

    'source_service_id',
    'source_service_name',
    'source_container_id',
    'source_node_id',

    'target_service_id',
    'target_service_name',
    'target_service_network_id',
    'target_service_expected_addr',

    'target_alias_result_alias'
  ]
)
PROMETHEUS_EXPORT_PORT = int(os.getenv('PROMETHEUS_EXPORT_PORT', '9000'))
DOCKER_HOSTNAME = os.getenv('DOCKER_HOSTNAME', platform.node())
# ten minutes
SCRAPE_INTERVAL = int(os.getenv('SCRAPE_INTERVAL', '600'))

RETRY_BACKOFF = int(os.getenv('RETRY_BACKOFF', '10'))
MAX_RETRIES_IN_ROW = int(os.getenv('MAX_RETRIES_IN_ROW', '10'))


DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

KNOWN_LABELS = dict()

def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'docker-service-dns-prometheus-exporter',
        msg)
    print(to_print)


PROXY_SERVICE_NAME = os.environ['PROXY_SERVICE_NAME']
DNS_CHECK_CONTAINER_IMAGE = os.getenv('DNS_CHECK_CONTAINER_IMAGE', 'ghcr.io/neuroforgede/docker-service-dns-prometheus-exporter/dnscheck:main')

def check_dns_in_cluster() -> List[ContainerNetworkTableResult]:
  ret: List[ContainerNetworkTableResult] = []
  from_env = docker.from_env(use_ssh_client=True)
  try:
    services = from_env.services.list()

    network_endpoints: List[ServiceEndpoint] = []
    network_aliases: List[NetworkAlias] = []

    def get_running_tasks(service):
      return [
          task
          for task in service.tasks()
          if  "Spec" in task 
          and "DesiredState" in task
          and task["DesiredState"] == "running"
          and "Status" in task
          and "State" in task["Status"]
          and task["Status"]["State"] == "running"
      ]


    for service in services:
      service_id = service.attrs["ID"]
      service_spec = service.attrs["Spec"]
      task_template = service_spec["TaskTemplate"]
      if "Networks" in task_template:
        running_tasks = get_running_tasks(service)
        if len(running_tasks) > 0:
          # ignore containers that are not started yet
          # mostly relevant for oneshot containers
          # with a restart condition that lets them act as a cronjob
          for network in task_template["Networks"]:
            network_id = network["Target"]
            network_aliases.append(NetworkAlias(
              service_id=service.attrs["ID"],
              service_name=service_spec["Name"],
              network_id=network_id,
              aliases=[service_spec["Name"], *network.get("Aliases", [])],
            ))
      
    network_aliases_by_service_and_network = group_by(lambda x: (x.service_id, x.network_id), network_aliases)

    for service in services:
      service_id=service.attrs["ID"]
      service_spec = service.attrs["Spec"]
      if "Endpoint" in service.attrs:
        service_endpoint = service.attrs["Endpoint"]
        if "VirtualIPs" in service_endpoint:
          for virtual_ip in service_endpoint["VirtualIPs"]:
            if "Addr" not in virtual_ip:
              continue
            aliases = network_aliases_by_service_and_network[(service_id, virtual_ip["NetworkID"])]
            if len(aliases) == 0:
              # ingress network
              continue
            network_endpoints.append(ServiceEndpoint(
              service_id=service.attrs["ID"],
              service_name=service_spec["Name"],
              network_id=virtual_ip["NetworkID"],
              addr=virtual_ip["Addr"],
              aliases=[item for alias in aliases for item in alias.aliases]
            ))


    # 1. get all service endpoints per network
    service_endpoints_by_network = group_by(lambda x: x.network_id, network_endpoints)

    # 2. get all service endpoints a service should reach
    service_endpoints_by_service = group_by(lambda x: x.service_id, network_endpoints)
    service_endpoints_service_should_reach: Dict[str, List[ServiceEndpoint]] = defaultdict(list)
    for service_id, network_endpoints_for_service in service_endpoints_by_service.items():
      for elem in network_endpoints_for_service:
        for service_endpoint in service_endpoints_by_network[elem.network_id]:
          service_endpoints_service_should_reach[service_id].append(service_endpoint)

    # 3. discover all tasks of services and construct their network table
    container_network_tables: List[ContainerNetworkTable] = []
    for service_id, endpoints_to_reach in service_endpoints_service_should_reach.items():
      service = from_env.services.get(service_id)
      service_name = service.attrs["Spec"]["Name"]
      running_tasks = get_running_tasks(service)
      for task in running_tasks:
        container_id = task["Status"]["ContainerStatus"]["ContainerID"]
        node_id = task["NodeID"]
        container_network_tables.append(ContainerNetworkTable(
          service_id=service_id,
          service_name=service_name,
          container_id=container_id,
          node_id=node_id,
          service_endpoints_to_reach=list(endpoints_to_reach)
        ))

    container_network_tables_by_node_id = group_by(lambda x: x.node_id, container_network_tables)

    # 4. connect to all available docker hosts and run the check
    answer = dns.resolver.resolve(f'tasks.{PROXY_SERVICE_NAME}', 'A')
    for rdata in answer:
        client = docker.DockerClient(base_url=f'tcp://{rdata.address}:2375')
        try:
          info = client.info()

          node_id = info["Swarm"]["NodeID"]

          container_network_tables_for_node_id = container_network_tables_by_node_id[node_id]
          for container_network_table in container_network_tables_for_node_id:
            print_timed(f'Checking Container {container_network_table.container_id} (service_name={container_network_table.service_name}, service_id={container_network_table.service_id}, container_id={container_network_table.container_id}, node_id={container_network_table.node_id})')
            # print(container_network_tables_for_node_id)
            res = subprocess.run(
              [
                "docker",
                "run",
                "--network",
                f"container:{container_network_table.container_id}",
                "--env",
                f"DEBUG={DEBUG}",
                "--env",
                f"CONTAINER_NETWORK_TABLE={container_network_table.to_json()}",
                "--rm",
                DNS_CHECK_CONTAINER_IMAGE,
                # cmd
                "dnscheck"
              ],
              # result output
              stdout=subprocess.PIPE,
              # debug output
              stderr=subprocess.PIPE,
              text=True,
              env={
                'DOCKER_HOST': 'tcp://' + str(rdata.address) + ':2375',
                **os.environ
              }
            )

            if res.returncode != 0:
              print(f"ERROR: subprocess failed with exit code: {res.returncode}")
              print(f"       commands: {res.args}")

              if DEBUG:
                print("STDOUT: ")
                print(res.stdout)

              if DEBUG:
                print("STDERR: ")
                print(res.stderr)


            if res.returncode == 0:
              check_result = ContainerNetworkTableResult.schema().loads(res.stdout)
              ret.append(check_result)
            elif 'cannot join network of a non running container' not in res.stdout:
              check_result = ContainerNetworkTableResult(
                container_id=container_network_table.container_id,
                node_id=container_network_table.node_id,
                service_id=container_network_table.service_id,
                service_name=container_network_table.service_name,
                results=[
                  ServiceEndpointCheckResult(
                    service_id=elem.service_id,
                    service_name=elem.service_name,
                    network_id=elem.network_id,
                    expected_addr=elem.addr,
                    alias_results=[
                      AliasResult(
                        alias=alias,
                        success=False,
                        message="failed to run dns healthcheck"
                      ) for alias in elem.aliases
                    ]
                  ) for elem in container_network_table.service_endpoints_to_reach
                ]
              )
              ret.append(check_result)
        except Exception as e:
          traceback.print_exc()
        finally:
          try:
            client.close()
          except Exception as e:
            traceback.print_exc()
  finally:
    try:
      from_env.close()
    except Exception as e:
      traceback.print_exc()
  
  return ret


def loop() -> None:
  global KNOWN_LABELS

  while not exit_event.is_set():
    if DEBUG:
      print_timed(f'before run - known Labels: {KNOWN_LABELS}')

    container_network_table_results = check_dns_in_cluster()

    old_known_labels = KNOWN_LABELS
    new_known_labels = dict()

    for container_network_table_result in container_network_table_results:
      for service_endpoint_check_result in container_network_table_result.results:
        for alias_result in service_endpoint_check_result.alias_results:
          labels = {
            'docker_hostname': DOCKER_HOSTNAME,

            'source_service_id': container_network_table_result.service_id,
            'source_service_name': container_network_table_result.service_name,
            'source_container_id': container_network_table_result.container_id,
            'source_node_id': container_network_table_result.node_id,

            'target_service_id': service_endpoint_check_result.service_id,
            'target_service_name': service_endpoint_check_result.service_name,
            'target_service_network_id': service_endpoint_check_result.network_id,
            'target_service_expected_addr': service_endpoint_check_result.expected_addr,

            'target_alias_result_alias': alias_result.alias
          }
          frozen_labels = frozenset(labels.items())

          new_known_labels[frozen_labels] = labels

          DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS.labels(**labels).set(
            1 if alias_result.success else 0
          )

    KNOWN_LABELS = new_known_labels

    if DEBUG:
      print_timed(f'after run - known Labels: {KNOWN_LABELS}')

    labels_to_remove = old_known_labels.keys() - new_known_labels.keys()

    for label_key in labels_to_remove:
      if DEBUG:
        print_timed(f"cleaning up prometheus labels (label_key={label_key}, labels={old_known_labels[label_key]})")
      DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS.remove(**old_known_labels[label_key])
    
    exit_event.wait(SCRAPE_INTERVAL)

def main() -> None:
  print_timed(f'Start prometheus client on port {PROMETHEUS_EXPORT_PORT}')
  start_http_server(PROMETHEUS_EXPORT_PORT, addr='0.0.0.0')
  
  failure_count = 0
  last_failure: Optional[datetime]
  while not exit_event.is_set():
    try:
      print_timed('starting loop')
      loop()
    except docker.errors.APIError:
      now = datetime.now()
      traceback.print_exc()

      if last_failure is not None and last_failure < (now - timedelta.seconds(SCRAPE_INTERVAL * 10)):
        print_timed("detected docker APIError, but last error was a bit back, resetting failure count.")
        # last failure was a while back, reset
        failure_count = 0

      failure_count += 1
      if failure_count > MAX_RETRIES_IN_ROW:
        print_timed(f"failed {failure_count} in a row. exit_eventing...")
        exit(1)

      last_failure = now
      print_timed(f"waiting {SCRAPE_INTERVAL} until next cycle")
      exit_event.wait(SCRAPE_INTERVAL)


if __name__ == '__main__':
  main()
