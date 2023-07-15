import dns.resolver
import os
import sys

from contract import *

DEBUG = os.environ.get('DEBUG', 'false') == 'true'

container_network_table_str: str = os.environ['CONTAINER_NETWORK_TABLE']
container_network_table: ContainerNetworkTable = ContainerNetworkTable.schema().loads(container_network_table_str)

def can_find_dns_entry(dns_entry: str) -> bool:
  answer = dns.resolver.resolve(dns_entry, 'A')
  for _ in answer:    
    return True
  return False

result = ContainerNetworkTableResult = ContainerNetworkTableResult(
   container_id=container_network_table.container_id,
   results=[]
)

for service_endpoint_to_find in container_network_table.service_endpoints_to_reach:
  if DEBUG:
    print(f"checking: {service_endpoint_to_find.service_name}({service_endpoint_to_find.service_id})", file=sys.stderr)
  cur_result = ServiceEndpointCheckResult(alias_results=[])
  for alias in service_endpoint_to_find.aliases:
    found_dns_tasks = can_find_dns_entry(alias)
    cur_result.alias_results.append(AliasResult(
      alias=alias,
      success=found_dns_tasks,
      message=None if found_dns_tasks else f"did not find dns entry for {alias}"
    ))
    if DEBUG:
      print(f"{alias}: {found_dns_tasks}", file=sys.stderr)

    result.results.append(cur_result)
        
print(result.to_json())