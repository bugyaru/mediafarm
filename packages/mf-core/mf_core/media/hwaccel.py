"""Capabilities воркера. Для L40: 3×NVENC/3×NVDEC, AV1 encode, лимита сессий нет."""
from __future__ import annotations

import asyncio
import platform
import re
from dataclasses import dataclass, field
from typing import Any

from mf_core.config import Settings, get_settings
from mf_core.telemetry import GPU_MEM, GPU_UTIL, get_logger

log = get_logger("mf.hw")

_CODECS_LINE = re.compile(r"^\s*([VAS][F.][S.][X.][B.][D.])\s+(\S+)")
_FILTER_LINE = re.compile(r"^\s*[TSC.]{3}\s+(\S+)\s+")


async def _text(cmd: list[str], timeout: int = 25) -> str:
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode("utf-8", "replace")
    except (TimeoutError, FileNotFoundError, OSError):
        return ""


def parse_codecs(text: str) -> set[str]:
    return {m.group(2) for line in text.splitlines() if (m := _CODECS_LINE.match(line))}


def parse_filters(text: str) -> set[str]:
    return {m.group(1) for line in text.splitlines() if (m := _FILTER_LINE.match(line))}


def parse_hwaccels(text: str) -> set[str]:
    out: set[str] = set()
    started = False
    for line in text.splitlines():
        if "Hardware acceleration methods" in line:
            started = True
            continue
        if started and line.strip() and not line.startswith(" "):
            break
        if started and line.strip():
            out.add(line.strip())
    return out


@dataclass(slots=True)
class GpuInfo:
    index: int
    name: str
    total_mem_mb: int
    free_mem_mb: int
    uuid: str | None = None
    compute_cap: str | None = None
    encoder_util: int | None = None
    decoder_util: int | None = None
    gpu_util: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "name": self.name, "total_mb": self.total_mem_mb,
            "free_mb": self.free_mem_mb, "uuid": self.uuid, "compute_cap": self.compute_cap,
            "encoder_util": self.encoder_util, "decoder_util": self.decoder_util,
            "gpu_util": self.gpu_util,
        }


@dataclass(frozen=True, slots=True)
class HwDecision:
    use_cuda: bool
    reason: str = "ok"
    gpu_index: int = 0
    decode_cuda: bool = True          # False → CPU-декод + hwupload_cuda
    scale_filter: str = "scale"
    deint_filter: str = "bwdif"
    upload_from_cpu: bool = False


@dataclass(slots=True)
class Hardware:
    cuda: bool = False
    gpus: list[GpuInfo] = field(default_factory=list)
    encoders: set[str] = field(default_factory=set)
    decoders: set[str] = field(default_factory=set)
    filters: set[str] = field(default_factory=set)
    hwaccels: set[str] = field(default_factory=set)
    driver: str | None = None
    cuda_version: str | None = None
    ffmpeg_version: str | None = None
    session_limit: int = 0            # 0 = без лимита (L40/professional)
    min_free_mb: int = 2000
    arch: str = field(default_factory=platform.machine)

    @property
    def nvenc(self) -> set[str]:
        return {c for c in self.encoders if c.endswith("_nvenc")}

    @property
    def nvdec(self) -> set[str]:
        return {c for c in self.decoders if c.endswith("_cuvid")}

    @property
    def cuda_filters(self) -> set[str]:
        return {f for f in self.filters if f.endswith(("_cuda", "_npp"))}

    @property
    def npp(self) -> bool:
        return "scale_npp" in self.filters

    def can_encode(self, codec: str) -> bool:
        return codec in self.encoders

    def can_decode(self, codec: str) -> bool:
        return f"{codec}_cuvid" in self.decoders or codec in self.nvdec

    def scale_filter(self) -> str:
        if not self.cuda:
            return "scale"
        return "scale_npp" if self.npp else "scale_cuda"

    def deint_filter(self) -> str:
        if not self.cuda:
            return "bwdif"
        for f in ("bwdif_cuda", "yadif_cuda"):
            if f in self.filters:
                return f
        return "bwdif"

    def labels(self) -> dict[str, str]:
        lb = {"arch": self.arch}
        if self.cuda:
            lb["gpu"] = "nvidia"
            lb["nvenc"] = ",".join(sorted(self.nvenc))
            lb["nvdec"] = ",".join(sorted(self.nvdec))
            if self.gpus:
                lb["gpu_model"] = self.gpus[0].name
        return lb

    def capabilities(self) -> dict[str, Any]:
        return {
            "cuda": self.cuda, "driver": self.driver, "cuda_version": self.cuda_version,
            "ffmpeg_version": self.ffmpeg_version, "session_limit": self.session_limit,
            "gpus": [g.as_dict() for g in self.gpus],
            "encoders": sorted(self.encoders), "cuda_filters": sorted(self.cuda_filters),
            "hwaccels": sorted(self.hwaccels), "npp": self.npp,
        }

    def decide(self, step: Any, info: Any) -> HwDecision:
        video = getattr(step, "video", None)
        if getattr(step, "hwaccel", "auto") == "cpu":
            return HwDecision(False, "forced_cpu")
        if video is not None and getattr(video, "copy", False):
            return HwDecision(False, "stream_copy")
        if not self.cuda:
            return HwDecision(False, "no_cuda")
        target = getattr(video, "codec", "h264_nvenc") if video else "h264_nvenc"
        if target not in self.nvenc:
            return HwDecision(False, f"no_encoder:{target}")
        v = getattr(info, "video", None)
        if v is not None and v.rotation:
            return HwDecision(False, "rotation")
        if v is not None and v.hdr and (getattr(step, "model_extra", None) or {}).get("tonemap"):
            return HwDecision(False, "tonemap_cpu")
        if self.gpus:
            best = max(self.gpus, key=lambda g: g.free_mem_mb)
            if best.free_mem_mb < self.min_free_mb:
                return HwDecision(False, "low_vram")
            idx = best.index
        else:
            idx = 0
        decode_cuda = bool(v is not None and self.can_decode(v.codec))
        return HwDecision(
            True, "ok", gpu_index=idx, decode_cuda=decode_cuda,
            scale_filter=self.scale_filter(), deint_filter=self.deint_filter(),
            upload_from_cpu=not decode_cuda,
        )

    def suggest_gpu_slots(self, per_job_mb: int = 1500) -> int:
        if not self.cuda or not self.gpus:
            return 0
        by_mem = sum(max(1, g.total_mem_mb // per_job_mb) for g in self.gpus)
        slots = min(8, int(by_mem))
        if self.session_limit > 0:
            slots = min(slots, self.session_limit)
        return max(1, slots)


def _nvml_sync() -> tuple[list[GpuInfo], str | None]:
    import pynvml

    pynvml.nvmlInit()
    driver = pynvml.nvmlSystemGetDriverVersion()
    try:
        cuda_ver = str(pynvml.nvmlSystemGetCudaDriverVersion_v2())
    except Exception:  # noqa: BLE001
        cuda_ver = None
    gpus: list[GpuInfo] = []
    for i in range(pynvml.nvmlDeviceGetCount()):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
        name = pynvml.nvmlDeviceGetName(h)
        if isinstance(name, bytes):
            name = name.decode()

        def safe(fn, default=None):
            try:
                return fn(h)
            except Exception:  # noqa: BLE001
                return default

        enc = safe(pynvml.nvmlDeviceGetEncoderUtilization)
        dec = safe(pynvml.nvmlDeviceGetDecoderUtilization)
        util = safe(pynvml.nvmlDeviceGetUtilizationRates)
        cc = safe(pynvml.nvmlDeviceGetCudaComputeCapability)
        gpus.append(GpuInfo(
            index=i, name=name,
            total_mem_mb=int(mem.total // 1048576), free_mem_mb=int(mem.free // 1048576),
            uuid=safe(pynvml.nvmlDeviceGetUUID),
            compute_cap=f"{cc[0]}.{cc[1]}" if cc else None,
            encoder_util=enc[0] if enc else None,
            decoder_util=dec[0] if dec else None,
            gpu_util=util.gpu if util else None,
        ))
    _ = cuda_ver
    return gpus, driver


async def detect_hardware(settings: Settings | None = None) -> Hardware:
    s = settings or get_settings()
    enc_t, dec_t, flt_t, hw_t, ver_t = await asyncio.gather(
        _text([s.ffmpeg_path, "-hide_banner", "-encoders"]),
        _text([s.ffmpeg_path, "-hide_banner", "-decoders"]),
        _text([s.ffmpeg_path, "-hide_banner", "-filters"]),
        _text([s.ffmpeg_path, "-hide_banner", "-hwaccels"]),
        _text([s.ffmpeg_path, "-hide_banner", "-version"]),
    )
    hw = Hardware(
        encoders=parse_codecs(enc_t), decoders=parse_codecs(dec_t),
        filters=parse_filters(flt_t), hwaccels=parse_hwaccels(hw_t),
        ffmpeg_version=(ver_t.splitlines() or [""])[0] or None,
        session_limit=s.nvenc_session_limit, min_free_mb=s.gpu_min_free_mem_mb,
    )
    if s.kind == "cuda":
        try:
            hw.gpus, hw.driver = await asyncio.to_thread(_nvml_sync)
        except Exception as e:  # noqa: BLE001
            log.warning("nvml_unavailable", error=str(e))
    hw.cuda = bool(hw.gpus) and bool(hw.nvenc) and "cuda" in hw.hwaccels
    if s.kind == "cuda" and not hw.cuda:
        log.error("cuda_requested_but_unavailable", gpus=len(hw.gpus),
                  nvenc=sorted(hw.nvenc), hwaccels=sorted(hw.hwaccels))
    log.info("hardware_detected", cuda=hw.cuda, gpus=[g.name for g in hw.gpus],
             nvenc=sorted(hw.nvenc), cuda_filters=sorted(hw.cuda_filters))
    return hw


async def gpu_snapshot(hw: Hardware) -> list[dict[str, Any]]:
    """Обновляет free/util и отдаёт снимок для heartbeat/метрик."""
    if not hw.cuda:
        return []
    try:
        gpus, _ = await asyncio.to_thread(_nvml_sync)
    except Exception:  # noqa: BLE001
        return [g.as_dict() for g in hw.gpus]
    hw.gpus = gpus
    for g in gpus:
        GPU_UTIL.labels(str(g.index), "gpu").set(g.gpu_util or 0)
        GPU_UTIL.labels(str(g.index), "encoder").set(g.encoder_util or 0)
        GPU_UTIL.labels(str(g.index), "decoder").set(g.decoder_util or 0)
        GPU_MEM.labels(str(g.index), "free").set(g.free_mem_mb * 1048576)
        GPU_MEM.labels(str(g.index), "total").set(g.total_mem_mb * 1048576)
    return [g.as_dict() for g in gpus]