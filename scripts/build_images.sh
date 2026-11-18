#!/usr/bin/env bash
set -euo pipefail

# --- Конфигурация ---
TAG="${TAG:-dev}"
REGISTRY="${REGISTRY:-mediafarm}"
PUSH="${PUSH:-0}"
PLATFORM="${PLATFORM:-linux/amd64}"
NO_CACHE="${NO_CACHE:-1}" # 1 = собирать без кэша, 0 = использовать кэш

# --- Подготовка флагов ---
if [ "$NO_CACHE" = "1" ]; then
  CACHE_FLAG="--no-cache"
  echo "⚠️  Сборка БЕЗ КЭША (--no-cache)"
else
  CACHE_FLAG=""
  echo "✅ Сборка с использованием кэша"
fi

echo "🏗️  Целевая платформа: $PLATFORM"
echo "----------------------------------------"

# --- Функция сборки ---
build() { 
  echo "🔨 Building $1..."
  # shellcheck disable=SC2086
  docker buildx build --platform "$PLATFORM" $CACHE_FLAG --load -t "$1" -f "$2" "${@:3}" .
}

# --- Проверка зависимостей ---
uv lock --check 2>/dev/null || uv lock

# --- Сборка App Base (Python окружение) ---
for svc in ui watcher worker; do
  build "${REGISTRY}/app-base:${svc}-${TAG}" docker/app-base.Dockerfile --build-arg SERVICE="${svc}"
done

# --- Сборка Media Base (FFmpeg, FFmbc, CUDA) ---
# Самые тяжелые образы. Сборка cuda под amd64 на ARM-машине может занять часы.
#build "${REGISTRY}/media-base:cpu-${TAG}" docker/media-base.Dockerfile --target cpu --build-arg INSTALL_FFMBC=1
#build "${REGISTRY}/media-base:cuda-${TAG}" docker/media-base.Dockerfile --target cuda --build-arg INSTALL_FFMBC=1

# --- Сборка конечных сервисов ---
build "${REGISTRY}/ui:${TAG}" docker/ui.Dockerfile --build-arg APP_BASE="${REGISTRY}/app-base:ui-${TAG}"
#build "${REGISTRY}/watcher:${TAG}" docker/watcher.Dockerfile --build-arg APP_BASE="${REGISTRY}/app-base:watcher-${TAG}"
#build "${REGISTRY}/worker:${TAG}-cpu" docker/worker.Dockerfile \
#  --build-arg APP_BASE="${REGISTRY}/app-base:worker-${TAG}" \
#  --build-arg MEDIA_BASE="${REGISTRY}/media-base:cpu-${TAG}"
#build "${REGISTRY}/worker:${TAG}-cuda" docker/worker.Dockerfile \
#  --build-arg APP_BASE="${REGISTRY}/app-base:worker-${TAG}" \
#  --build-arg MEDIA_BASE="${REGISTRY}/media-base:cuda-${TAG}"
#build "${REGISTRY}/migrations:${TAG}" docker/migrations.Dockerfile

# --- Push в Registry (опционально) ---
if [ "$PUSH" = "1" ]; then
  for img in "ui:${TAG}" "watcher:${TAG}" "worker:${TAG}-cpu" "worker:${TAG}-cuda" "migrations:${TAG}"; do
    docker push "${REGISTRY}/${img}"
  done
fi

echo "🎉 Сборка завершена!"