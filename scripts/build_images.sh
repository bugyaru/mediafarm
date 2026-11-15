#!/usr/bin/env bash
set -euo pipefail
TAG="${TAG:-dev}"
REGISTRY="${REGISTRY:-mediafarm}"
PUSH="${PUSH:-0}"
BUILDER="${BUILDER:-docker}"

build() { docker buildx build --load -t "$1" -f "$2" "${@:3}" .; }

uv lock --check 2>/dev/null || uv lock

for svc in ui watcher worker; do
  build "${REGISTRY}/app-base:${svc}-${TAG}" docker/app-base.Dockerfile --build-arg SERVICE="${svc}"
done

build "${REGISTRY}/media-base:cpu-${TAG}" docker/media-base.Dockerfile --target cpu --build-arg INSTALL_FFMBC=1
build "${REGISTRY}/media-base:cuda-${TAG}" docker/media-base.Dockerfile --target cuda --build-arg INSTALL_FFMBC=1

build "${REGISTRY}/ui:${TAG}" docker/ui.Dockerfile --build-arg APP_BASE="${REGISTRY}/app-base:ui-${TAG}"
build "${REGISTRY}/watcher:${TAG}" docker/watcher.Dockerfile --build-arg APP_BASE="${REGISTRY}/app-base:watcher-${TAG}"
build "${REGISTRY}/worker:${TAG}-cpu" docker/worker.Dockerfile \
  --build-arg APP_BASE="${REGISTRY}/app-base:worker-${TAG}" \
  --build-arg MEDIA_BASE="${REGISTRY}/media-base:cpu-${TAG}"
build "${REGISTRY}/worker:${TAG}-cuda" docker/worker.Dockerfile \
  --build-arg APP_BASE="${REGISTRY}/app-base:worker-${TAG}" \
  --build-arg MEDIA_BASE="${REGISTRY}/media-base:cuda-${TAG}"
build "${REGISTRY}/migrations:${TAG}" docker/migrations.Dockerfile

if [ "$PUSH" = "1" ]; then
  for img in "ui:${TAG}" "watcher:${TAG}" "worker:${TAG}-cpu" "worker:${TAG}-cuda" "migrations:${TAG}"; do
    docker push "${REGISTRY}/${img}"
  done
fi