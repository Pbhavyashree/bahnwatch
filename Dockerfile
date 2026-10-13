FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY web ./web

RUN pip install --no-cache-dir -e .

# Simulator by default so the container is useful with no configuration and no
# outbound network. Set BAHNWATCH_SIMULATE=0 for the live BVG feed.
ENV BAHNWATCH_SIMULATE=1 \
    BAHNWATCH_POLL_SECONDS=20 \
    BAHNWATCH_DB=/data/bahnwatch.db

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://localhost:8000/api/state')); sys.exit(0 if d.get('status')=='ok' else 1)"

CMD ["uvicorn", "bahnwatch.api:app", "--host", "0.0.0.0", "--port", "8000"]
