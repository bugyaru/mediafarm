import asyncio
import sys

import pytest

from mf_core.errors import CancelledByUser, StallError
from mf_core.media.progress import ProgressParser, run_media


def test_parser_microseconds_quirk():
    p = ProgressParser(duration_s=100.0)
    assert p.feed("frame=100") is None
    assert p.feed("fps=50.0") is None
    snap = p.feed("out_time_ms=25000000")     # это 25 секунд, не 25000
    assert snap is None
    p.feed("speed=2.5x")
    snap = p.feed("progress=continue")
    assert snap["out_time_s"] == 25.0 and snap["pct"] == 25.0
    assert snap["speed"] == 2.5 and snap["eta_s"] == 30.0
    assert p.feed("progress=end") is not None


def test_parser_prefers_out_time_us():
    p = ProgressParser(duration_s=10)
    p.feed("out_time_us=5000000")
    p.feed("out_time_ms=999")
    snap = p.feed("progress=continue")
    assert snap["out_time_s"] == 5.0 and snap["pct"] == 50.0


def _py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


EMITTER = """
import sys, time
for i in range(1, 6):
    print(f"frame={i*25}"); print(f"out_time_us={i*1000000}")
    print("speed=5x"); print("progress=continue"); sys.stdout.flush()
    print("warning: synthetic", file=sys.stderr); sys.stderr.flush()
    time.sleep(0.05)
print("progress=end"); sys.stdout.flush()
"""


async def test_run_media_success_and_progress():
    seen = []
    res = await run_media(_py(EMITTER), duration_s=5.0,
                          on_progress=lambda p: seen.append(p), progress_interval_s=0.0)
    assert res.returncode == 0 and seen[-1]["done"] is True
    assert res.avg_speed == 5.0
    assert any("synthetic" in line for line in res.stderr_tail)


async def test_run_media_nonzero_raises_classified():
    from mf_core.errors import SourceInvalidError
    code = "import sys; print('moov atom not found', file=sys.stderr); sys.exit(1)"
    with pytest.raises(SourceInvalidError):
        await run_media(_py(code))


async def test_run_media_stall_watchdog():
    with pytest.raises(StallError):
        await run_media(_py("import time; time.sleep(10)"), stall_timeout_s=1)


async def test_run_media_cancel():
    ev = asyncio.Event()

    async def cancel_soon():
        await asyncio.sleep(0.3)
        ev.set()

    task = asyncio.create_task(cancel_soon())
    with pytest.raises(CancelledByUser):
        await run_media(_py("import time; time.sleep(10)"), cancel=ev, stall_timeout_s=30)
    await task