FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 dotusd \
    && useradd --uid 10001 --gid dotusd --no-create-home --shell /usr/sbin/nologin dotusd \
    && install -d -o dotusd -g dotusd /data

COPY release.py server.py gap_repair.py archive_reader.py hub_quotes.py issuance.py treasury.py ./
COPY VERSION about-dotusd.html CHANGELOG.md index.html app.js ranges.js style.css favicon.svg social-preview.png ./
ARG DEPLOY_COMMIT
RUN python -c 'import os, pathlib, re; commit = os.environ.get("DEPLOY_COMMIT", ""); assert re.fullmatch("[0-9a-f]{40}", commit), "DEPLOY_COMMIT must be a full Git SHA"; pathlib.Path("DEPLOY_COMMIT").write_text(commit + "\n")'

USER 10001:10001
VOLUME ["/data"]
EXPOSE 3001
# /health answers 503 until the first finalized head is read and once it is over
# 180 s old, so an unreachable RPC or stalled finality also marks this unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:3001/health", timeout=3)'
CMD ["python", "server.py", "--bind", "0.0.0.0", "--port", "3001", "--no-browser", "--hub-rpc", "https://polkadot-asset-hub-rpc.polkadot.io", "--archive", "/data/archive.sqlite3", "--gap-repair-history", "/data/pool-repairs.sqlite3"]
