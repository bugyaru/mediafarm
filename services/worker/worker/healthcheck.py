"""Docker/K8s HEALTHCHECK без внешних зависимостей."""
from __future__ import annotations

import sys
import urllib.request

from mf_core.config import get_settings


def main() -> int:
    s = get_settings()
    url = f"http://127.0.0.1:{s.metrics_port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310
            return 0 if r.status == 200 else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())