FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY argus ./argus

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client curl ca-certificates && \
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir -e .

CMD ["python", "-m", "argus.scripts.ingest_vault"]
