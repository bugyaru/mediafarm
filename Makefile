TAG ?= dev
REGISTRY ?= mediafarm

images:            ; TAG=$(TAG) REGISTRY=$(REGISTRY) bash scripts/build_images.sh
vendor:            ; bash scripts/vendor_assets.sh
compose-up:        ; docker compose -f docker/docker-compose.yml up -d --build
compose-down:      ; docker compose -f docker/docker-compose.yml down
compose-logs:      ; docker compose -f docker/docker-compose.yml logs -f --tail=100
e2e:               ; bash scripts/e2e_compose.sh
helm-lint:         ; helm lint deploy/helm/mediafarm
helm-template:     ; helm template mf deploy/helm/mediafarm | kubeconform -strict -summary