FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates ripgrep && rm -rf /var/lib/apt/lists/*
WORKDIR /app
