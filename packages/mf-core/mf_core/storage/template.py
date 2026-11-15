"""Шаблоны путей публикации: {yyyy}/{mm}/{asset.stem}_{preset.name}.mp4"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from mf_core.errors import ConfigInvalidError

_TOKEN = re.compile(r"\{([a-zA-Z0-9_.]+)\}")
_SAFE = re.compile(r"[^\w.\-]+", re.UNICODE)


def sanitize(part: str, maxlen: int = 180) -> str:
    return _SAFE.sub("_", part).strip("._")[:maxlen] or "file"


def build_context(
        *, asset_uri: str = "", asset_meta: dict | None = None, preset_name: str = "",
        job_id: str = "", now: dt.datetime | None = None, extra: dict | None = None,
) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.UTC)
    name = asset_uri.rstrip("/").rsplit("/", 1)[-1] if asset_uri else "file"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    ctx: dict[str, Any] = {
        "yyyy": now.strftime("%Y"), "yy": now.strftime("%y"),
        "mm": now.strftime("%m"), "dd": now.strftime("%d"),
        "hh": now.strftime("%H"), "date": now.strftime("%Y-%m-%d"),
        "ts": int(now.timestamp()),
        "asset": {"name": sanitize(name), "stem": sanitize(stem), "uri": asset_uri},
        "preset": {"name": sanitize(preset_name)},
        "job": {"id": str(job_id)},
    }
    for k, v in (asset_meta or {}).items():
        ctx.setdefault("meta", {})[str(k)] = sanitize(str(v))
    ctx.update(extra or {})
    return ctx


def render(template: str, ctx: dict[str, Any]) -> str:
    def sub(m: re.Match) -> str:
        cur: Any = ctx
        for part in m.group(1).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                raise ConfigInvalidError(f"неизвестный токен шаблона: {{{m.group(1)}}}")
        return str(cur)

    out = _TOKEN.sub(sub, template).lstrip("/")
    if ".." in out.split("/"):
        raise ConfigInvalidError("шаблон пути не должен содержать '..'")
    return out