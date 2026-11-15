from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mf_core.errors import ConfigInvalidError

Engine = Literal["ffmpeg", "ffmbc", "ytdlp", "python"]
STEP_IDS = ("acquire", "probe", "transcode", "thumbnail", "verify", "publish", "notify")
GPU_ENGINES = {"ffmpeg"}          # ffmbc/yt-dlp не используют CUDA напрямую


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ----------------------------- preset spec ---------------------------------
class ScaleSpec(BaseModel):
    width: int | None = None
    height: int | None = None
    mode: Literal["fit", "fill", "exact"] = "fit"
    keep_aspect: bool = True
    hw: Literal["auto", "cuda", "cpu"] = "auto"

    @model_validator(mode="after")
    def _any_dim(self):
        if not self.width and not self.height:
            raise ValueError("scale: нужен width или height")
        return self


class VideoSpec(BaseModel):
    codec: str = "h264_nvenc"
    cpu_codec: str = "libx264"
    rc: Literal["vbr", "cbr", "cq", "constqp"] = "vbr"
    bitrate: str | None = "6M"
    maxrate: str | None = None
    bufsize: str | None = None
    cq: int | None = None
    preset: str | None = "p5"
    profile: str | None = None
    gop: int | None = 50
    bframes: int | None = 2
    pix_fmt: str = "yuv420p"
    copy: bool = False


class AudioSpec(BaseModel):
    codec: str = "aac"
    bitrate: str = "192k"
    channels: int | None = 2
    sample_rate: int | None = 48000
    loudnorm: Literal["off", "ebu_r128"] = "off"
    copy: bool = False
    map: str = "0:a?"


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    engine: Engine | None = None
    hwaccel: Literal["auto", "cuda", "cpu"] = "auto"
    fallback_to_cpu: bool = True
    mode: Literal["stream", "staged"] = "staged"
    resume: bool = True
    video: VideoSpec | None = None
    audio: list[AudioSpec] = Field(default_factory=list)
    scale: ScaleSpec | None = None
    deinterlace: Literal["off", "auto", "force"] = "auto"
    container: str | None = None
    extra_out: list[str] = Field(default_factory=list)
    extra_in: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    storage: str | None = None
    path: str | None = None
    on_conflict: Literal["skip", "overwrite", "version"] = "skip"
    at: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in STEP_IDS:
            raise ValueError(f"неизвестный шаг {v!r}; допустимы {STEP_IDS}")
        return v


class PresetSpec(BaseModel):
    name: str
    version: int = 1
    description: str | None = None
    selector: dict[str, str] = Field(default_factory=dict)
    requires_gpu: bool = False
    gpu_sessions: int = 1
    cpu_cost: int = 1
    max_attempts: int = 3
    timeout_s: int | None = None
    steps: list[StepSpec]

    @model_validator(mode="after")
    def _validate(self):
        ids = [s.id for s in self.steps]
        if not ids:
            raise ValueError("preset: пустой список шагов")
        if len(set(ids)) != len(ids):
            raise ValueError("preset: id шагов должны быть уникальны")
        if "publish" in ids and "acquire" not in ids and "transcode" not in ids:
            raise ValueError("preset: publish требует acquire или transcode")
        engines = {s.engine for s in self.steps if s.engine}
        # ffmbc/yt-dlp не умеют CUDA — GPU-требование снимаем автоматически
        if engines and not (engines & GPU_ENGINES):
            object.__setattr__(self, "requires_gpu", False)
            object.__setattr__(self, "selector", {k: v for k, v in self.selector.items() if k != "gpu"})
        if self.requires_gpu:
            self.selector.setdefault("gpu", "nvidia")
        for s in self.steps:
            if s.id == "publish" and not (s.storage and s.path):
                raise ValueError("шаг publish: обязательны storage и path")
            if s.id == "transcode" and s.video is None:
                raise ValueError("шаг transcode: обязателен блок video")
        return self

    def to_db(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    @classmethod
    def from_db(cls, raw: dict[str, Any]) -> PresetSpec:
        try:
            return cls.model_validate(raw)
        except Exception as e:
            raise ConfigInvalidError(f"некорректный preset spec: {e}") from e


# --------------------------------- DTO -------------------------------------
class StorageIn(BaseModel):
    name: str
    kind: Literal["local", "nfs", "smb", "s3", "ftp"]
    base_uri: str
    mount_path: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = None
    read_only: bool = False
    enabled: bool = True


class StorageOut(ORMModel, StorageIn):
    id: uuid.UUID
    created_at: dt.datetime
    updated_at: dt.datetime


class WatchFolderIn(BaseModel):
    name: str
    storage_id: uuid.UUID
    path: str = ""
    recursive: bool = True
    enabled: bool = True
    poll_interval_s: int = 30
    stability_s: int = 60
    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)
    ready_marker: str | None = None
    parser_id: str | None = None
    parser_opts: dict[str, Any] = Field(default_factory=dict)
    preset_id: uuid.UUID | None = None
    job_template: dict[str, Any] = Field(default_factory=dict)
    concurrency_key: str | None = None
    max_concurrency: int = 0
    archive_mode: Literal["none", "mark", "move"] = "none"
    archive_path: str | None = None


class WatchFolderOut(ORMModel, WatchFolderIn):
    id: uuid.UUID


class JobCreate(BaseModel):
    kind: Literal["transcode", "capture", "fetch", "probe"] = "transcode"
    asset_id: uuid.UUID | None = None
    url: str | None = None
    input_uri: str | None = None
    preset_id: uuid.UUID | None = None
    preset_name: str | None = None
    priority: int = 100
    params: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = 3
    run_after: dt.datetime | None = None

    @model_validator(mode="after")
    def _source(self):
        if not any([self.asset_id, self.url, self.input_uri]):
            raise ValueError("нужен один из: asset_id / url / input_uri")
        if not any([self.preset_id, self.preset_name]):
            raise ValueError("нужен preset_id или preset_name")
        return self


class JobOut(ORMModel):
    id: uuid.UUID
    kind: str
    state: str
    priority: int
    attempts: int
    max_attempts: int
    requires_gpu: bool
    worker_id: str | None
    progress: dict[str, Any]
    error: dict[str, Any]
    result: dict[str, Any]
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class WorkerOut(ORMModel):
    id: str
    kind: str
    state: str
    labels: dict[str, Any]
    capabilities: dict[str, Any]
    cpu_slots: int
    gpu_slots: int
    cpu_used: int
    gpu_used: int
    heartbeat_at: dt.datetime


class ScheduleIn(BaseModel):
    name: str
    cron: str
    timezone: str = "UTC"
    enabled: bool = True
    job_template: dict[str, Any]
    misfire_grace_s: int = 300

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        parts = v.split()
        if len(parts) not in (5, 6):
            raise ValueError("cron: ожидается 5 или 6 полей")
        return v