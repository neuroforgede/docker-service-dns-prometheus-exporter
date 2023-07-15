import dns.resolver
import os
import sys
import traceback

from contract import *

def main() -> None:
  DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

  container_network_table_str: str = os.environ['CONTAINER_NETWORK_TABLE']
  container_network_table: ContainerNetworkTable = ContainerNetworkTable.schema().loads(container_network_table_str)


  if DEBUG:
    print(f"Checking network reachability from {container_network_table.service_name} (service_id: {container_network_table.service_id}, container_id: {container_network_table.container_id})", file=sys.stderr)

  def can_find_dns_entry(dns_entry: str) -> bool:
    try:
      answer = dns.resolver.resolve(dns_entry, 'A')
      for _ in answer:    
        return True
      return False
    except dns.resolver.NXDOMAIN:
      if DEBUG:
        traceback.print_exc(file=sys.stderr)
      return False

  result = ContainerNetworkTableResult(
    node_id=container_network_table.node_id,
    service_id=container_network_table.service_id,
    service_name=container_network_table.service_name,
    container_id=container_network_table.container_id,
    results=[]
  )

  for service_endpoint_to_find in container_network_table.service_endpoints_to_reach:
    if DEBUG:
      print(f"checking: {service_endpoint_to_find.service_name} (service_id: {container_network_table.service_id}, container_id: {container_network_table.container_id})", file=sys.stderr)
    cur_result = ServiceEndpointCheckResult(
      service_name=service_endpoint_to_find.service_name,
      service_id=service_endpoint_to_find.service_id,
      expected_addr=service_endpoint_to_find.addr,
      network_id=service_endpoint_to_find.network_id,
      alias_results=[]
    )
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


if __name__ == '__main__':
  main()