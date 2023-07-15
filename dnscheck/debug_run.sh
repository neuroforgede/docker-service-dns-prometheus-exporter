docker build -f Dockerfile -t dnscheck:debug .
docker run --rm -it --network docker_swarm_proxy -p 127.0.0.1:10001:9000 --rm -v /var/run/docker.sock:/var/run/docker.sock dnscheck:debug