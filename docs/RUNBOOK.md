# MediaFarm runbook

## Развёртывание (compose)
```bash
uv lock
bash scripts/vendor_assets.sh
TAG=dev bash scripts/build_images.sh
docker compose -f docker/docker-compose.yml up -d
# UI: http://localhost:8080, пароль admin — в логах ui или MF_UI_BOOTSTRAP_PASSWORD