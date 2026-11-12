#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="ghcr.io/fenrircl/api-pdf:latest"
CONTAINER="api-pdf"
ENV_FILE="/etc/api-pdf/api-pdf.env"
HOST_PORT="8000"
LOCK_FILE="/run/lock/api-pdf-update.lock"

if [[ ! -r "$ENV_FILE" ]]; then
  echo "Missing environment file: $ENV_FILE" >&2
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "An api-pdf update is already running"
  exit 0
fi

OLD_IMAGE="$(docker inspect "$CONTAINER" --format '{{.Image}}' 2>/dev/null || true)"
OLD_HEALTHCHECK="$(docker inspect "$CONTAINER" --format '{{if .Config.Healthcheck}}yes{{else}}no{{end}}' 2>/dev/null || true)"
docker pull "$IMAGE"
NEW_IMAGE="$(docker image inspect "$IMAGE" --format '{{.Id}}')"

if [[ -n "$OLD_IMAGE" && "$OLD_IMAGE" == "$NEW_IMAGE" && "$OLD_HEALTHCHECK" == "yes" ]]; then
  echo "api-pdf is already current ($NEW_IMAGE)"
  exit 0
fi

run_api_pdf() {
  local image_ref="$1"
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --env-file "$ENV_FILE" \
    --publish "$HOST_PORT:8000" \
    --health-cmd='python -c "import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/health\")"' \
    --health-interval=15s \
    --health-timeout=5s \
    --health-retries=4 \
    "$image_ref"
}

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
if ! run_api_pdf "$IMAGE"; then
  echo "Could not start updated image; restoring previous image" >&2
  if [[ -n "$OLD_IMAGE" ]]; then run_api_pdf "$OLD_IMAGE"; fi
  exit 1
fi

for _ in $(seq 1 30); do
  STATUS="$(docker inspect "$CONTAINER" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || true)"
  if [[ "$STATUS" == "healthy" ]]; then
    echo "api-pdf updated successfully to $NEW_IMAGE"
    exit 0
  fi
  if [[ "$STATUS" == "unhealthy" || "$STATUS" == "exited" ]]; then
    break
  fi
  sleep 2
done

echo "Updated image failed its health check; restoring previous image" >&2
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
if [[ -n "$OLD_IMAGE" ]]; then run_api_pdf "$OLD_IMAGE"; fi
exit 1
