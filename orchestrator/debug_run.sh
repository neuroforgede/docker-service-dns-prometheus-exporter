docker build -f Dockerfile -t orchestrator:debug .
docker run --network docker_swarm_proxy --rm -v /var/run/docker.sock:/var/run/docker.sock orchestrator:debug