"""JSON-манифесты. Поддерживается JSONPath-lite: a.b[0].c, items[*].url."""
from __future__ import annotations

import json
import re
from typing import Any

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, SourceSpec, register, spec_from_row

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+|\*)\]")


def extract(obj: Any, path: str) -> Any:
    if path in ("", "$", "."):
        return obj
    cur = obj
    for name, idx in _TOKEN.findall(path.lstrip("$.")):
        if name:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(name)
        elif idx == "*":
            return cur if isinstance(cur, list) else None
        else:
            if not isinstance(cur, list):
                return None
            i = int(idx)
            cur = cur[i] if i < len(cur) else None
        if cur is None:
            return None
    return cur


@register("json")
class JsonParser:
    id = "json"

    async def parse(self, ctx: ParseContext) -> list[SourceSpec]:
        opts = ctx.options
        try:
            data = json.loads(await ctx.read_text(opts.get("encoding")))
        except json.JSONDecodeError as e:
            raise ConfigInvalidError(f"{ctx.entry.uri}: битый JSON ({e})") from e

        items = extract(data, opts.get("items_path", "$"))
        if items is None:
            return []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise ConfigInvalidError("json-парсер: items_path должен указывать на массив/объект")

        limit = int(opts.get("limit") or 0)
        if limit:
            items = items[:limit]
        fields: dict[str, str] = opts.get("fields") or {}
        out: list[SourceSpec] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = {name: extract(item, path) for name, path in fields.items()} if fields else dict(item)
            spec = spec_from_row(
                ctx, row,
                url_field=opts.get("url_field", "url"),
                url_template=opts.get("url_template"),
                preset_field=opts.get("preset_field", "preset"),
                priority_field=opts.get("priority_field", "priority"),
                identity_field=opts.get("identity_field", "id"),
                meta_fields=opts.get("meta_fields"),
                default_kind=opts.get("kind"),
            )
            if spec is not None:
                out.append(spec)
        return out