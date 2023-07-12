#!/usr/bin/python3
import docker
from typing import Dict, List, TypeVar, Callable, Any
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from functools import reduce
from collections import defaultdict
import json

T = TypeVar('T')
K = TypeVar('K')

def group_by(key: Callable[[T], K], seq: List[T]) -> Dict[K, List[T]]:
  return reduce(lambda grp, val: grp[key(val)].append(val) or grp, seq, defaultdict(list))


from_env = docker.from_env(use_ssh_client=True)
services = from_env.services.list()

@dataclass_json
@dataclass
class ServiceEndpoint:
  service_id: str
  service_name: str
  network_id: str
  addr: str
  aliases: List[str]


@dataclass_json
@dataclass
class ContainerNetworkTable:
  service_id: str
  service_name: str
  container_id: str
  node_id: str
  service_endpoints_to_reach: List[ServiceEndpoint]

@dataclass
class NetworkAlias:
  service_id: str
  service_name: str
  network_id: str
  aliases: List[str]

def dump_as_json_string(input: Dict[str, List[ServiceEndpoint]]) -> Dict[str, List[Any]]:
  as_dict = {k: [elem.to_dict() for elem in v] for k,v in input.items()}
  return json.dumps(as_dict, indent=4)

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
service_endpoints_by_network_as_json_string = dump_as_json_string(service_endpoints_by_network)
print(service_endpoints_by_network_as_json_string)

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



print(ContainerNetworkTable.schema().dumps(container_network_tables, indent=4, many=True))

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
