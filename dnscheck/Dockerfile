FROM --platform=${BUILDPLATFORM:-linux/amd64} python:3.10-slim
WORKDIR /code
COPY dnscheck.py dnscheck.py
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
ENTRYPOINT ["/bin/python3", "-m ", "dnscheck"]