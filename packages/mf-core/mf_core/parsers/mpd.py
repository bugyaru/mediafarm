from __future__ import annotations

import re
from typing import Any

from defusedxml import ElementTree as ET

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, SourceSpec, register

_DUR = re.compile(
    r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$"
)


def _duration(value: str | None) -> float | None:
    if not value:
        return None
    m = _DUR.match(value.strip())
    if not m:
        return None
    d, h, mi, s = m.groups()
    return (int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _findall(el, name: str):
    return [c for c in el if _local(c.tag) == name]


@register("mpd")
class MpdParser:
    id = "mpd"

    async def parse(self, ctx: ParseContext) -> list[SourceSpec]:
        raw = await ctx.read_bytes()
        try:
            root = ET.fromstring(raw)
        except Exception as e:  # noqa: BLE001
            raise ConfigInvalidError(f"{ctx.entry.uri}: битый MPD ({e})") from e
        if _local(root.tag) != "MPD":
            raise ConfigInvalidError(f"{ctx.entry.uri}: корень не MPD")

        duration = _duration(root.get("mediaPresentationDuration"))
        dynamic = (root.get("type") or "static").lower() == "dynamic"
        reps: list[dict[str, Any]] = []
        base_urls: list[str] = []

        for b in _findall(root, "BaseURL"):
            if b.text and b.text.strip().startswith(("http://", "https://")):
                base_urls.append(b.text.strip())

        for period in _findall(root, "Period"):
            for aset in _findall(period, "AdaptationSet"):
                common = {
                    "content_type": aset.get("contentType") or aset.get("mimeType"),
                    "lang": aset.get("lang"),
                    "period": period.get("id"),
                }
                for rep in _findall(aset, "Representation"):
                    reps.append({
                        **common,
                        "id": rep.get("id"),
                        "bandwidth": int(rep.get("bandwidth") or 0) or None,
                        "width": int(rep.get("width") or 0) or None,
                        "height": int(rep.get("height") or 0) or None,
                        "codecs": rep.get("codecs") or aset.get("codecs"),
                        "frame_rate": rep.get("frameRate"),
                    })

        meta = {
            "manifest": ctx.entry.uri, "duration_s": duration, "live": dynamic,
            "representations": reps,
            "languages": sorted({r["lang"] for r in reps if r.get("lang")}),
        }
        uri = base_urls[0] if base_urls else ctx.entry.uri
        kind = "url" if uri.startswith(("http://", "https://")) else "file"

        if ctx.options.get("mode") != "representations":
            return [SourceSpec(uri=uri, kind=kind,
                               meta={k: v for k, v in meta.items() if v is not None},
                               identity=f"{ctx.entry.uri}#manifest",
                               preset_name=ctx.options.get("preset_name"))]

        min_bw = int(ctx.options.get("min_bandwidth") or 0)
        out = []
        for r in reps:
            if (r.get("bandwidth") or 0) < min_bw:
                continue
            if r.get("content_type", "").startswith("audio") and ctx.options.get("video_only"):
                continue
            out.append(SourceSpec(
                uri=uri, kind=kind, meta={**meta, "representation": r},
                identity=f"{ctx.entry.uri}#{r.get('period')}:{r.get('id')}",
                preset_name=ctx.options.get("preset_name"),
            ))
        return out