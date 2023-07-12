#!/usr/bin/python3
import docker
from typing import Dict, List, TypeVar, Callable
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from functools import reduce
from collections import defaultdict

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

@dataclass
class NetworkAlias:
  service_id: str
  service_name: str
  network_id: str
  aliases: List[str]


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

service_endpoints_by_network = group_by(lambda x: x.network_id, network_endpoints)
as_json = ServiceEndpoint.schema().dumps(service_endpoints_by_network, many=True)
print(as_json)

# next steps:
# 1. go to every node using the network map we just constructed
# 2. for every container on the host do
# 2.1 for every network it is part of do
# 2.1.1 check every service that should be found from inside the container
#       and whether the IP matches via a script from inside the netns
#       docker run --rm --net=container:<target container> myimage:tag my_script.py --args
# 2.1.2 if its not, mark container as unhealthy w.r.t DNS
