# mediafarm

Для ручной сборки всех Docker-образов проекта `mediafarm` под архитектуру `linux/amd64` (x86_64) необходимо соблюдать строгую последовательность.

Дело в том, что конечные образы сервисов (`ui`, `worker`, `watcher`) используют многоступенчатую сборку (multi-stage) и наследуются от промежуточных базовых образов (`app-base` и `media-base`). Если базовых образов нет в локальном Docker Daemon, сборка конечных сервисов прервется с ошибкой `manifest unknown` или `pull access denied`.

Ниже приведена полная последовательность команд для терминала.

### 0. Подготовка зависимостей
Скрипты сборки ожидают наличие зафиксированного лога зависимостей `uv.lock` в корне проекта.
```bash
uv lock
```

### 1. Сборка базовых образов (App Base)
Эти образы содержат настроенное виртуальное окружение Python 3.12 и установленные зависимости для каждого конкретного микросервиса (через `uv sync --package`).
```bash
# UI (FastAPI + Uvicorn)
docker buildx build --platform linux/amd64 --load -t mediafarm/app-base:ui-dev \
  -f docker/app-base.Dockerfile --build-arg SERVICE=ui .

# Watcher (Сканер файловых систем)
docker buildx build --platform linux/amd64 --load -t mediafarm/app-base:watcher-dev \
  -f docker/app-base.Dockerfile --build-arg SERVICE=watcher .

# Worker (Вычислительная нода)
docker buildx build --platform linux/amd64 --load -t mediafarm/app-base:worker-dev \
  -f docker/app-base.Dockerfile --build-arg SERVICE=worker .
```

### 2. Сборка базовых образов (Media Base)
Эти образы самые тяжелые. В них компилируется/устанавливается FFmpeg, FFmbc, а для CUDA-версии — драйверы и тулчейн NVIDIA.
```bash
# CPU-версия медиа-окружения (FFmpeg + FFmbc)
docker buildx build --platform linux/amd64 --load -t mediafarm/media-base:cpu-dev \
  -f docker/media-base.Dockerfile --target cpu --build-arg INSTALL_FFMBC=1 .

# GPU-версия медиа-окружения (с поддержкой NVENC/CUDA)
docker buildx build --platform linux/amd64 --load -t mediafarm/media-base:cuda-dev \
  -f docker/media-base.Dockerfile --target cuda --build-arg INSTALL_FFMBC=1 .
```

### 3. Сборка конечных образов сервисов
Теперь, когда базовые слои лежат в локальном кэше Docker, финальные `Dockerfile` соберутся практически мгновенно, так как им нужно лишь скопировать исходный код (source code) и точки входа (entrypoints).
```bash
# UI
docker buildx build --platform linux/amd64 --load -t mediafarm/ui:dev \
  -f docker/ui.Dockerfile --build-arg APP_BASE=mediafarm/app-base:ui-dev .

# Watcher
docker buildx build --platform linux/amd64 --load -t mediafarm/watcher:dev \
  -f docker/watcher.Dockerfile --build-arg APP_BASE=mediafarm/app-base:watcher-dev .

# Worker (CPU)
docker buildx build --platform linux/amd64 --load -t mediafarm/worker:dev-cpu \
  -f docker/worker.Dockerfile \
  --build-arg APP_BASE=mediafarm/app-base:worker-dev \
  --build-arg MEDIA_BASE=mediafarm/media-base:cpu-dev .

# Worker (CUDA)
docker buildx build --platform linux/amd64 --load -t mediafarm/worker:dev-cuda \
  -f docker/worker.Dockerfile \
  --build-arg APP_BASE=mediafarm/app-base:worker-dev \
  --build-arg MEDIA_BASE=mediafarm/media-base:cuda-dev .

# Миграции БД (Alembic)
docker buildx build --platform linux/amd64 --load -t mediafarm/migrations:dev \
  -f docker/migrations.Dockerfile .
```

или
```shell
./scripts/build_images.sh
```
---