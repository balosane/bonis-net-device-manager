FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY bonis_netdev ./bonis_netdev
RUN pip install --no-cache-dir .
ENTRYPOINT ["netdev"]
