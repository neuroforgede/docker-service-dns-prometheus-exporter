#!/usr/bin/python3
import docker
from docker.models import services as docker_services
from typing import Dict, List, TypeVar, Callable, Any, Tuple
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
import yaml
import re

SERVICE_REGEX_IGNORE_LIST: List[str] = yaml.safe_load(os.getenv('SERVICE_REGEX_IGNORE_LIST', '[]'))

def is_service_relevant(service_name: str) -> bool:
  if SERVICE_REGEX_IGNORE_LIST is not None and isinstance(SERVICE_REGEX_IGNORE_LIST, list):
    for regex in SERVICE_REGEX_IGNORE_LIST:
      match = re.match(regex, service_name)
      if match is not None:
        return False
  return True

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

docker_service_dns_resolution_success_labels = [
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

APP_NAME = "Docker DNS Check Exporter"
DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS = Gauge('docker_service_dns_resolution_success',
  'Docker Service DNS Resolution Success',
  docker_service_dns_resolution_success_labels
)
PROMETHEUS_EXPORT_PORT = int(os.getenv('PROMETHEUS_EXPORT_PORT', '9000'))
DOCKER_HOSTNAME = os.getenv('DOCKER_HOSTNAME', platform.node())
# ten minutes
SCRAPE_INTERVAL = int(os.getenv('SCRAPE_INTERVAL', '600'))

RETRY_BACKOFF = int(os.getenv('RETRY_BACKOFF', '10'))
MAX_RETRIES_IN_ROW = int(os.getenv('MAX_RETRIES_IN_ROW', '10'))


DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'
DEBUG_DUMP_LABELS = os.environ.get('DEBUG_DUMP_LABELS', 'false').lower() == 'true'

KNOWN_LABELS = dict()

def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'docker-service-dns-prometheus-exporter',
        msg)
    print(to_print)


PROXY_SERVICE_NAME = os.environ['PROXY_SERVICE_NAME']
DNS_CHECK_CONTAINER_IMAGE = os.getenv('DNS_CHECK_CONTAINER_IMAGE', 'ghcr.io/neuroforgede/docker-service-dns-prometheus-exporter/dnscheck:main')

def get_running_tasks(service: List[docker_services.Service]) -> List[Any]:
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


def get_network_aliases_by_service_and_network(services: List[docker_services.Service]) -> Dict[Tuple[str, str], List[NetworkAlias]]:
  network_aliases: List[NetworkAlias] = []
  for service in services:
    service_id = service.attrs["ID"]
    service_spec = service.attrs["Spec"]
    task_template = service_spec["TaskTemplate"]
    running_tasks = get_running_tasks(service)
    if len(running_tasks) == 0:
      continue
    if "Networks" in task_template:
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
    
  ret = group_by(lambda x: (x.service_id, x.network_id), network_aliases)
  return ret


def get_service_endpoints_by_network(
    services: List[docker_services.Service],
    network_aliases_by_service_and_network: Dict[Tuple[str, str], List[NetworkAlias]]
  ) -> Dict[str, List[ServiceEndpoint]]:
  """
    get all reachable services and create Service Endpoints for every relevant one
    we try to filter out services that are not checkable.
    These include services that dont have a valid DNS entry
    or services that don't have a virtual IP.
    While dnsrr services might be releavnt for DNS checks as well, these services are
    much more complex in their behaviour. At the moment, we don't want to
    generate a list of all IPs that should match the DNS.
  """
  service_endpoints: List[ServiceEndpoint] = []
  for service in services:
    service_id = service.attrs["ID"]
    service_spec = service.attrs["Spec"]
    if "Endpoint" in service.attrs:
      service_endpoint = service.attrs["Endpoint"]
      if "VirtualIPs" in service_endpoint:
        for virtual_ip in service_endpoint["VirtualIPs"]:
          if "Addr" not in virtual_ip:
            continue
          aliases = network_aliases_by_service_and_network[(service_id, virtual_ip["NetworkID"])]
          if len(aliases) == 0:
            # ingress network, or no running tasks
            continue
          service_endpoints.append(ServiceEndpoint(
            service_id=service.attrs["ID"],
            service_name=service_spec["Name"],
            network_id=virtual_ip["NetworkID"],
            addr=virtual_ip["Addr"],
            aliases=[item for alias in aliases for item in alias.aliases]
          ))
  ret = group_by(lambda x: x.network_id, service_endpoints)
  return ret


def get_service_endpoints_service_should_reach(
    services: List[docker_services.Service],
    service_endpoints_by_network: Dict[str, List[ServiceEndpoint]]
) -> Dict[str, List[ServiceEndpoint]]:
    """
      for each service with running tasks construct a list of endpoints it should
      reach by looking at all networks it is part of and then adding the relevant
      service_endpoints to its list
    """
    service_endpoints_service_should_reach: Dict[str, List[ServiceEndpoint]] = defaultdict(list)
    for service in services:
      service_id = service.attrs["ID"]
      service_spec = service.attrs["Spec"]
      task_template = service_spec["TaskTemplate"]
      running_tasks = get_running_tasks(service)
      if len(running_tasks) == 0:
        continue
      if "Networks" in task_template:
        for network in task_template["Networks"]:
          network_id = network["Target"]
          for service_endpoint in service_endpoints_by_network[network_id]:
            service_endpoints_service_should_reach[service_id].append(service_endpoint)
    return service_endpoints_service_should_reach


def get_network_tables(
    docker_client: docker.DockerClient,
    service_endpoints_service_should_reach: Dict[str, List[ServiceEndpoint]]
  ) -> List[ContainerNetworkTable]:
  """
    discover all tasks of services and construct their network table
  """
  container_network_tables: List[ContainerNetworkTable] = []
  for service_id, endpoints_to_reach in service_endpoints_service_should_reach.items():
    service = docker_client.services.get(service_id)
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
  return container_network_tables


def check_dns_in_cluster() -> List[ContainerNetworkTableResult]:
  ret: List[ContainerNetworkTableResult] = []
  from_env = docker.from_env(use_ssh_client=True)
  try:
    services = from_env.services.list()
    services = [service for service in services if is_service_relevant(service.attrs["Spec"]["Name"])]

    network_aliases_by_service_and_network = get_network_aliases_by_service_and_network(services)

    service_endpoints_by_network = get_service_endpoints_by_network(
      services=services,
      network_aliases_by_service_and_network=network_aliases_by_service_and_network
    )

    service_endpoints_service_should_reach = get_service_endpoints_service_should_reach(
      services=services,
      service_endpoints_by_network=service_endpoints_by_network
    )

    container_network_tables = get_network_tables(
      docker_client=from_env,
      service_endpoints_service_should_reach=service_endpoints_service_should_reach
    )

    # group network tables by node id so we can do the lookup per node
    container_network_tables_by_node_id = group_by(lambda x: x.node_id, container_network_tables)

    # finally, connect to all available docker hosts and run the check
    # FIXME: if we don't find the docker host for the a node, mark all containers as failed?
    answer = dns.resolver.resolve(f'tasks.{PROXY_SERVICE_NAME}', 'A')
    for rdata in answer:
        client = docker.DockerClient(base_url=f'tcp://{rdata.address}:2375')
        try:
          info = client.info()

          node_id = info["Swarm"]["NodeID"]

          container_network_tables_for_node_id = container_network_tables_by_node_id[node_id]
          for container_network_table in container_network_tables_for_node_id:
            print_timed(f'Checking Container {container_network_table.container_id} (service_name={container_network_table.service_name}, service_id={container_network_table.service_id}, container_id={container_network_table.container_id}, node_id={container_network_table.node_id})')
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
              try:
                check_result = ContainerNetworkTableResult.schema().loads(res.stdout)
                ret.append(check_result)
              except json.decoder.JSONDecodeError:
                print("failed to parse into ContainerNetworkTableResult " + res.stdout)
            elif 'cannot join network of a non running container' not in res.stderr:
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
    if DEBUG and DEBUG_DUMP_LABELS:
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

    if DEBUG and DEBUG_DUMP_LABELS:
      print_timed(f'after run - known Labels: {KNOWN_LABELS}')

    labels_to_remove = old_known_labels.keys() - new_known_labels.keys()

    for label_key in labels_to_remove:
      if DEBUG:
        print_timed(f"cleaning up prometheus labels (label_key={label_key}, labels={old_known_labels[label_key]})")
      values = old_known_labels[label_key]
      DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS.remove(*[values[label_elem] for label_elem in docker_service_dns_resolution_success_labels])
    
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
