FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/workhome
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-dev.txt pyproject.toml README.md ./
COPY src/ src/
COPY tools/ tools/
COPY scripts/ scripts/
RUN pip install --no-cache-dir -e ".[dev]" ollama
RUN useradd --create-home --home-dir /workhome --shell /bin/bash jarvis \
    && mkdir -p /workhome/jobs /workhome/repo \
    && chown -R jarvis:jarvis /app /workhome
USER jarvis
ENTRYPOINT ["python", "scripts/autonomous_job.py"]
