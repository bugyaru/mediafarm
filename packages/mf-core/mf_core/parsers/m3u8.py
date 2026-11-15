from __future__ import annotations

import re
from typing import Any

from mf_core.errors import ConfigInvalidError
from mf_core.parsers import ParseContext, SourceSpec, register

_ATTR = re.compile(r"([A-Z0-9-]+)=(\"[^\"]*\"|[^,]*)")


def _attrs(line: str) -> dict[str, str]:
    payload = line.split(":", 1)[1] if ":" in line else ""
    return {k: v.strip().strip('"') for k, v in _ATTR.findall(payload)}


@register("m3u8")
class M3U8Parser:
    id = "m3u8"

    async def parse(self, ctx: ParseContext) -> list[SourceSpec]:
        text = await ctx.read_text()
        if "#EXTM3U" not in text:
            raise ConfigInvalidError(f"{ctx.entry.uri}: не m3u8-манифест")
        if "#EXT-X-STREAM-INF" in text:
            return self._master(ctx, text)
        return self._media(ctx, text)

    def _master(self, ctx: ParseContext, text: str) -> list[SourceSpec]:
        opts = ctx.options
        variants: list[tuple[dict[str, Any], str]] = []
        pending: dict[str, Any] | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                a = _attrs(line)
                w, _, h = (a.get("RESOLUTION") or "").partition("x")
                pending = {
                    "variant": True,
                    "bandwidth": int(a["BANDWIDTH"]) if a.get("BANDWIDTH", "").isdigit() else None,
                    "width": int(w) if w.isdigit() else None,
                    "height": int(h) if h.isdigit() else None,
                    "codecs": a.get("CODECS"),
                    "frame_rate": a.get("FRAME-RATE"),
                    "name": a.get("NAME"),
                    "manifest": ctx.entry.uri,
                }
            elif line and not line.startswith("#") and pending is not None:
                variants.append((pending, line))
                pending = None

        min_bw = int(opts.get("min_bandwidth") or 0)
        max_h = int(opts.get("max_height") or 100000)
        picked = [
            (m, u) for m, u in variants
            if (m["bandwidth"] or 0) >= min_bw and (m["height"] or 0) <= max_h
        ]
        if opts.get("best_only") and picked:
            picked = [max(picked, key=lambda p: p[0]["bandwidth"] or 0)]

        out: list[SourceSpec] = []
        for meta, ref in picked:
            uri = ctx.resolve(ref)
            out.append(SourceSpec(
                uri=uri,
                kind="url" if uri.startswith(("http://", "https://")) else "file",
                meta={k: v for k, v in meta.items() if v is not None},
                identity=f"{ctx.entry.uri}#{meta.get('name') or ref}",
                preset_name=ctx.options.get("preset_name"),
            ))
        return out

    def _media(self, ctx: ParseContext, text: str) -> list[SourceSpec]:
        opts = ctx.options
        segments: list[tuple[float, str]] = []
        duration = 0.0
        encrypted = False
        pending = 0.0
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line.upper():
                encrypted = True
            elif line.startswith("#EXTINF"):
                try:
                    pending = float(line.split(":", 1)[1].split(",")[0])
                except (ValueError, IndexError):
                    pending = 0.0
            elif line and not line.startswith("#"):
                segments.append((pending, line))
                duration += pending
                pending = 0.0

        if encrypted and opts.get("skip_encrypted", True):
            return []
        meta = {
            "playlist": ctx.entry.uri, "duration_s": round(duration, 3),
            "segments": len(segments), "encrypted": encrypted,
            "live": "#EXT-X-ENDLIST" not in text,
        }
        if not opts.get("segments"):
            uri = ctx.entry.uri
            return [SourceSpec(
                uri=uri, kind="url" if uri.startswith(("http://", "https://")) else "file",
                meta=meta, identity=f"{ctx.entry.uri}#playlist",
                preset_name=opts.get("preset_name"),
            )]
        out = []
        for i, (dur, ref) in enumerate(segments):
            uri = ctx.resolve(ref)
            out.append(SourceSpec(
                uri=uri, kind="file" if "://" not in uri or uri.startswith("file:") else "url",
                meta={**meta, "segment_index": i, "segment_duration_s": dur},
                identity=f"{ctx.entry.uri}#{i}:{ref}",
                preset_name=opts.get("preset_name"),
            ))
        return out