import dns.resolver
import os
import json
from dataclasses import dataclass
from dataclasses_json import dataclass_json
from typing import List

@dataclass_json
@dataclass
class ServiceEndpoint:
  service_id: str
  service_name: str
  network_id: str
  addr: str
  aliases: List[str]


service_endpoints_to_find_str: str = os.environ['SERVICE_ENDPOINTS_TO_FIND']

service_endpoints_to_find: List[ServiceEndpoint] = ServiceEndpoint.schema().loads(service_endpoints_to_find_str, many=True)

def can_find_dns_entry(dns_entry: str) -> bool:
    answer = dns.resolver.resolve(dns_entry, 'A')
    for _ in answer:    
        return True
    print(f"didn't find entry for {dns_entry}")
    return False

for service_endpoint_to_find in service_endpoints_to_find:
    found_dns_tasks = can_find_dns_entry(service_endpoint_to_find.service_name)
    #
        
