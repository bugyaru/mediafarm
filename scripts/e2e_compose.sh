#!/usr/bin/env bash
set -euo pipefail
COMPOSE="docker compose -f docker/docker-compose.yml"
$COMPOSE up -d --build postgres migrate ui watcher worker-cpu
trap '$COMPOSE logs --tail=80; $COMPOSE down -v' EXIT

for i in $(seq 1 60); do
  curl -fsS http://localhost:8080/healthz >/dev/null && break
  sleep 2
done

mkdir -p docker/data/in docker/data/out
$COMPOSE exec -T worker-cpu ffmpeg -hide_banner -nostdin -v error -y \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=440 -t 4 \
  -c:v libx264 -preset ultrafast -c:a aac /data/in/e2e.mp4

python - <<'PY'
import os, time, httpx
base = "http://localhost:8080"
c = httpx.Client(base_url=base, follow_redirects=False, timeout=30)
r = c.post("/login", data={"username":"admin","password":os.getenv("MF_UI_BOOTSTRAP_PASSWORD","admin-password-123")})
assert r.status_code == 303, r.text
from mf_core.config import get_settings
from ui.security import csrf_token
token = csrf_token(c.cookies.get("mf_session"), get_settings())
h = {"X-CSRF-Token": token}
assert c.post("/api/v1/presets/seed", headers=h).status_code == 200
ins = c.post("/api/v1/storages", headers=h, json={"name":"e2e_in","kind":"local","base_uri":"file:///data/in"}).json()
outs = c.post("/api/v1/storages", headers=h, json={"name":"e2e_out","kind":"local","base_uri":"file:///data/out"}).json()
preset = c.get("/api/v1/presets").json()[0]
wf = c.post("/api/v1/watch-folders", headers=h, json={
  "name":"e2e","storage_id":ins["id"],"path":"","preset_id":preset["id"],
  "include":["*.mp4"],"stability_s":1,"poll_interval_s":5,
  "job_template":{"preset_name":preset["name"],"params":{"output":{"storage":"e2e_out","path":"{asset.stem}_out.mp4"}}},
}).json()
c.post(f"/api/v1/watch-folders/{wf['id']}/scan-now", headers=h)
deadline = time.time() + 180
while time.time() < deadline:
    jobs = c.get("/api/v1/jobs").json()
    if jobs and jobs[0]["state"] == "succeeded":
        print("E2E OK:", jobs[0]["id"]); break
    if jobs and jobs[0]["state"] == "failed":
        raise SystemExit(f"E2E FAILED: {jobs[0]['error']}")
    time.sleep(2)
else:
    raise SystemExit("E2E timeout")
PY
test -n "$(find docker/data/out -name '*.mp4' -print -quit)"
echo "✅ e2e passed"