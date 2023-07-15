from typing import Optional, List
from dataclasses import dataclass
from dataclasses_json import dataclass_json

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

@dataclass_json
@dataclass
class AliasResult:
  alias: str
  success: bool
  message: Optional[str]

@dataclass_json
@dataclass
class ServiceEndpointCheckResult:
  alias_results: List[AliasResult]

@dataclass_json
@dataclass
class ContainerNetworkTableResult:
  container_id: str
  results: List[ServiceEndpointCheckResult]