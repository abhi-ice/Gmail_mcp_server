FROM python:3.12-slim

# Minimal system deps; sqlite3 is part of stdlib but needs the shared library
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir -e .

# Data dir is mounted as a volume; default path matches config.py default
ENV GMAIL_MCP_DATA_DIR=/data \
    GMAIL_MCP_TRANSPORT=http \
    GMAIL_MCP_HTTP_HOST=0.0.0.0 \
    GMAIL_MCP_HTTP_PORT=8765

VOLUME ["/data"]
EXPOSE 8765

CMD ["gmail-mcp"]
