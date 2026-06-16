# Build + run the decrypt-shim PoC on Linux (LD_PRELOAD is Linux-only; the
# macOS host can't run it directly). Mirrors the style of
# code_execution_server/Dockerfile.gvisor-test.
#
#   docker build -t db-decrypt-poc -f db_encryption_poc/Dockerfile db_encryption_poc
#   docker run --rm db-decrypt-poc
#
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev libssl-dev sqlite3 xxd \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir cryptography

WORKDIR /poc
COPY . /poc
RUN chmod +x run_test.sh

CMD ["./run_test.sh"]
