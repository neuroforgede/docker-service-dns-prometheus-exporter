FROM --platform=${BUILDPLATFORM:-linux/amd64} docker:24.0.2-cli
RUN apk add python3 py-pip
WORKDIR /code
ENV PYTHONUNBUFFERED=1
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
COPY . .
ENTRYPOINT ["/usr/bin/python3", "-m"]
CMD ["orchestrator"]