FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir ansible-core==2.17.14 librouteros \
    && ansible-galaxy collection install community.routeros:==3.21.0 -p /usr/share/ansible/collections
COPY pyproject.toml README.md ./
COPY bonis_netdev ./bonis_netdev
RUN pip install --no-cache-dir .
COPY config ./config
COPY ansible ./ansible
ENV ANSIBLE_COLLECTIONS_PATH=/app/ansible/collections:/usr/share/ansible/collections
ENTRYPOINT ["netdev"]
