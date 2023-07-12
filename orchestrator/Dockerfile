FROM --platform=${BUILDPLATFORM:-linux/amd64} docker:24.0.2-cli
RUN apk add python3 py-pip
WORKDIR /code
COPY orchestrator.py orchestrator.py
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
ENTRYPOINT ["/usr/bin/python3", "-m ", "main"]