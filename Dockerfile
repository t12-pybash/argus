FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY argus ./argus

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir -e .

CMD ["python", "-m", "argus.scripts.ingest_vault"]
