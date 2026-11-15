from __future__ import annotations

import asyncio
import shutil

from mf_core.config import Settings, get_settings
from mf_core.media.hwaccel import Hardware, detect_hardware


async def _version(cmd: list[str]) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 15)
        return (out.decode("utf-8", "replace").splitlines() or [""])[0][:120] or None
    except (TimeoutError, OSError):
        return None


async def detect(settings: Settings | None = None) -> tuple[Hardware, dict, dict]:
    s = settings or get_settings()
    hw = await detect_hardware(s)
    caps = hw.capabilities()
    caps["tools"] = {
        "ffmpeg": hw.ffmpeg_version,
        "ffprobe": await _version([s.ffprobe_path, "-version"]),
        "yt-dlp": await _version([s.ytdlp_path, "--version"]),
        "ffmbc": await _version([s.ffmbc_path, "-version"]) if s.ffmbc_enabled else None,
    }
    caps["ffmbc_enabled"] = s.ffmbc_enabled
    return hw, hw.labels(), caps
