"""Универсальный XML-маппинг (broadcast sidecar, NLE-экспорты, новостные выгрузки).

options:
  item_xpath: ".//{*}Clip"          # обязателен
  namespaces: {"n": "urn:..."}      # опционально
  fields: {clip_id: "@id", title: "n:Title/text()"}
  url_field: "url" | url_template: "{clip_id}.mxf"
  preset_field / priority_field / identity_field / meta_fields
"""
from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as ET

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, SourceSpec, register, spec_from_row


def _extract(el, path: str, ns: dict[str, str] | None) -> str | None:
    """ElementTree XPath ограничен: атрибуты поддерживаем вручную."""
    path = path.strip()
    if path.startswith("@"):
        return el.get(path[1:])
    if "/@" in path:
        head, _, attr = path.rpartition("/@")
        sub = el.find(head, ns) if head else el
        return sub.get(attr) if sub is not None else None
    if path.endswith("/text()"):
        path = path[: -len("/text()")]
    if not path or path == ".":
        return (el.text or "").strip() or None
    sub = el.find(path, ns)
    if sub is None:
        return None
    return (sub.text or "").strip() or None


@register("xml")
class XmlParser:
    id = "xml"

    async def parse(self, ctx: ParseContext) -> list[SourceSpec]:
        opts = ctx.options
        item_xpath = opts.get("item_xpath")
        if not item_xpath:
            raise ConfigInvalidError("xml-парсер: нужен item_xpath")
        ns = opts.get("namespaces") or None
        try:
            root = ET.fromstring(await ctx.read_bytes())
        except Exception as e:  # noqa: BLE001
            raise ConfigInvalidError(f"{ctx.entry.uri}: битый XML ({e})") from e

        fields: dict[str, str] = opts.get("fields") or {}
        out: list[SourceSpec] = []
        for el in root.findall(item_xpath, ns):
            row: dict[str, Any] = {}
            for name, path in fields.items():
                val = _extract(el, path, ns)
                if val is not None:
                    row[name] = val
            if not row and (el.text or "").strip():
                row["value"] = el.text.strip()
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