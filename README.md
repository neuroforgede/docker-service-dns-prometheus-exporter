# docker-service-dns-prometheus-exporter

Monitor your Docker Swarm for DNS resolution errors and export it to Prometheus.

## Use in a Docker Swarm deployment

Deploy:

```yaml
version: "3.8"

services:
  docker_socket_proxy:
    image: tecnativa/docker-socket-proxy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    networks:
      - net
    environment:
      CONTAINERS: 1
      SERVICES: 1
      SWARM: 1
      NODES: 1
      NETWORKS: 1
      TASKS: 1
      VERSION: 1

      AUTH: 1
      SECRETS: 1
      POST: 1
      BUILD: 1
      COMMIT: 1
      CONFIGS: 1
      DISTRIBUTION: 1
      EXEC: 1
      GRPC: 1
      IMAGES: 1
      INFO: 1
      PLUGINS: 1
      SESSION: 1
      SYSTEM: 1
      VOLUMES: 1
    deploy:
      mode: global

  docker-service-dns-prometheus-exporter:
    image: ghcr.io/neuroforgede/docker-service-dns-prometheus-exporter/dnscheck:0.1.11
    environment:
      - PROXY_SERVICE_NAME=monitoring_docker_socket_proxy
      - DNS_CHECK_CONTAINER_IMAGE=ghcr.io/neuroforgede/docker-service-dns-prometheus-exporter/dnscheck:0.1.11
      - DEBUG=true
    volumes:
      - '/var/run/docker.sock:/var/run/docker.sock'
    networks:
      - net
    deploy:
      mode: replicated
      replicas: 1
      resources:
        limits:
          memory: 256M
        reservations:
          memory: 128M
      placement:
        constraints:
          - node.role==manager
```

prometheus.yml

```yaml
# ...
scrape_configs:
  - job_name: 'docker-service-dns-prometheus-exporter'
    dns_sd_configs:
    - names:
      - 'tasks.docker-service-dns-prometheus-exporter'
      type: 'A'
      port: 9000
```
