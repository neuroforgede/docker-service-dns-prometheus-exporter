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


DEBUG = os.environ.get('DEBUG', 'false') == 'true'


def print_timed(msg):
    to_print = '{} [{}]: {}'.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'docker_events',
        msg)
    print(to_print)


PROXY_SERVICE_NAME = 'proxies_socket_proxy'
DNS_CHECK_CONTAINER_IMAGE = 'dnscheck:debug'

def check_dns_in_cluster() -> List[ContainerNetworkTableResult]:
  ret: List[ContainerNetworkTableResult] = []
  from_env = docker.from_env(use_ssh_client=True)
  try:
    services = from_env.services.list()

    network_endpoints: List[ServiceEndpoint] = []
    network_aliases: List[NetworkAlias] = []

    for service in services:
      service_id=service.attrs["ID"]
      service_spec = service.attrs["Spec"]
      task_template = service_spec["TaskTemplate"]
      if "Networks" in task_template:
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

    # 3. discover all docker hosts via swarm proxy
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
              text=True
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

            check_result = ContainerNetworkTableResult.schema().loads(res.stdout)
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
  while not exit_event.is_set():
    container_network_table_results = check_dns_in_cluster()

    for container_network_table_result in container_network_table_results:
      for service_endpoint_check_result in container_network_table_result.results:
        for alias_result in service_endpoint_check_result.alias_results:
          DOCKER_SERVICE_DNS_RESOLUTION_SUCCESS.labels(**{
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
          }).set(
            1 if alias_result.success else 0
          )
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

  # next: go to the nodes and run the check
  # careful: this relies on the network we are testing to see if it works
  # TODO: find a better way to handle this

  # next steps:
  # 1. go to every node using the network map we just constructed
  # 2. for every container on the host do
  # 2.1 for every network it is part of do
  # 2.1.1 check every service that should be found from inside the container
  #       and whether the IP matches via a script from inside the netns
  #       docker run --rm --net=container:<target container> myimage:tag my_script.py --args
  # 2.1.2 if its not, mark container as unhealthy w.r.t DNS