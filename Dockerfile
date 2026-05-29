FROM python:3.12-alpine

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sre ./sre

RUN pip install --no-cache-dir -e .

CMD ["python", "-m", "sre.scripts.ingest_vault"]
