#!/usr/bin/env bash
set -euo pipefail
DIR="services/ui/ui/static/vendor"
mkdir -p "$DIR"
curl -fsSL https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js -o "$DIR/htmx.min.js"
curl -fsSL https://unpkg.com/htmx-ext-ws@2.0.2/ws.js -o "$DIR/htmx-ws.js"
curl -fsSL https://unpkg.com/alpinejs@3.14.8/dist/cdn.min.js -o "$DIR/alpine.min.js"
echo "vendored → $DIR"