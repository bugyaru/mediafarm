import uuid
from pathlib import Path

import pytest

from mf_core.config import Settings
from mf_core.errors import HWAccelError
from mf_core.media.hwaccel import Hardware
from mf_core.media.presets import validate_spec
from mf_core.queue import ClaimedJob
from mf_core.storage import StorageRegistry
from worker.context import JobContext
from worker.pipeline import Pipeline

SPEC = validate_spec({
    "name": "t", "steps": [{"id": "acquire"}, {"id": "transcode", "engine": "ffmpeg",
                                               "fallback_to_cpu": True,
                                               "video": {"codec": "h264_nvenc"}},
                           {"id": "publish", "storage": "out", "path": "{asset.stem}.mp4"}],
})


class FakeSF:
    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return self


def make_ctx(tmp_path) -> JobContext:
    import datetime as dt
    job = ClaimedJob(uuid.uuid4(), "transcode", 1, 3, None, None, SPEC.to_db(), {}, None,
                     None, False, 0, 1, None, dt.datetime.now(dt.UTC), 100)
    return JobContext(job=job, spec=SPEC, settings=Settings(scratch=str(tmp_path)),
                      registry=StorageRegistry(), hw=Hardware(cuda=False),
                      session_factory=FakeSF(), workdir=tmp_path)


async def test_hw_fallback_retries_on_cpu(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    calls = []

    async def acquire(step, c):
        return {"input": "x"}

    async def transcode(step, c):
        calls.append(step.hwaccel)
        if step.hwaccel != "cpu":
            raise HWAccelError("nvenc gone", reason="no_cuda")
        return {"path": str(tmp_path / "o.mp4"), "hw": False}

    async def publish(step, c):
        return {"uri": "out/o.mp4"}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ctx, "step_start", noop)
    monkeypatch.setattr(ctx, "step_finish", noop)
    monkeypatch.setattr(ctx, "event", noop)

    p = Pipeline(ctx, {"acquire": acquire, "transcode": transcode, "publish": publish})
    res = await p.run()
    assert calls == ["auto", "cpu"]
    assert res["output"] == {"uri": "out/o.mp4"}


async def test_no_fallback_when_disabled(tmp_path, monkeypatch):
    spec = SPEC.model_copy(deep=True)
    spec.steps[1].fallback_to_cpu = False
    ctx = make_ctx(tmp_path)
    ctx.spec = spec

    async def acquire(step, c):
        return {}

    async def transcode(step, c):
        raise HWAccelError("boom")

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ctx, "step_start", noop)
    monkeypatch.setattr(ctx, "step_finish", noop)
    with pytest.raises(HWAccelError):
        await Pipeline(ctx, {"acquire": acquire, "transcode": transcode}).run()


def test_weighted_progress(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.set_step(1)                       # transcode
    base = ctx.progress["pct"]
    ctx.s.progress_throttle_s = 0
    ctx.on_progress({"pct": 50})
    assert base < ctx.progress["pct"] < 100
    assert ctx.progress["step"] == "transcode"
    