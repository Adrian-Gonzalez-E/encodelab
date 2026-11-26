#!/usr/bin/env bash
# Arranca el fixture, ejecuta check_ui.py en Playwright (contenedor desechable) y para el fixture.
# Uso (en el host): app/e2e/run.sh [directorio de capturas]
set -u
cd "$(dirname "$0")/../.."
export MEDIA_PATH=${MEDIA_PATH:-/tmp}  # el fixture no usa medios reales
export ENCODELAB_TAG=${ENCODELAB_TAG:-dev}  # imagen local: docker build -t ghcr.io/adrian-gonzalez-e/encodelab:dev app
OUT=${1:-/tmp/encodelab-e2e}; mkdir -p "$OUT"; chmod 777 "$OUT"; rm -f "$OUT"/*.png
docker rm -f encodelab-e2e >/dev/null 2>&1
docker compose run -d --rm --no-deps --name encodelab-e2e encodelab python3 -m e2e.fixture_server >/dev/null 2>&1
for i in $(seq 1 20); do docker exec encodelab-e2e python3 -c "import urllib.request;urllib.request.urlopen('http://localhost:8099/metricas')" 2>/dev/null && break; sleep 0.5; done
docker run --rm --network container:encodelab-e2e -v "$PWD/app/e2e:/e2e:ro" -v "$OUT:/out" mcr.microsoft.com/playwright/python:v1.55.0-noble \
  sh -c "pip install -q --timeout 30 --retries 3 --break-system-packages playwright==1.55.0 >/dev/null 2>&1 || pip install -q --timeout 30 --retries 3 playwright==1.55.0 >/dev/null; python /e2e/check_ui.py"
rc=$?
docker logs encodelab-e2e 2>&1 | grep -E "Traceback|Error" | head -5
docker stop encodelab-e2e >/dev/null 2>&1
exit $rc
