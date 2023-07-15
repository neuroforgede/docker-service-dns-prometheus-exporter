docker build -f Dockerfile -t dnscheck:debug .
docker run --env PROXY_SERVICE_NAME=proxies_socket_proxy \
    --env DEBUG=true \
    --env DNS_CHECK_CONTAINER_IMAGE=dnscheck:debug \
    --rm \
    -it \
    --network docker_swarm_proxy \
    -p 127.0.0.1:10001:9000 \
    --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    dnscheck:debug