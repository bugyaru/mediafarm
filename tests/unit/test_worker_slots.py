import uuid

from mf_core.queue import ClaimedJob
from worker.slots import Slots


def job(cpu=1, gpu=0, requires_gpu=False) -> ClaimedJob:
    import datetime as dt
    return ClaimedJob(
        id=uuid.uuid4(), kind="transcode", attempts=1, max_attempts=3, preset_id=None,
        preset_version=None, spec={}, params={}, asset_id=None, watch_folder_id=None,
        requires_gpu=requires_gpu, gpu_sessions=gpu, cpu_cost=cpu, concurrency_key=None,
        lease_expires_at=dt.datetime.now(dt.UTC), priority=100,
    )


def test_reserve_release():
    s = Slots(cpu_total=4, gpu_total=2)
    j = job(cpu=2, gpu=1, requires_gpu=True)
    assert s.can_fit(j.cpu_cost, j.gpu_sessions, j.requires_gpu)
    s.reserve(j)
    assert (s.cpu_used, s.gpu_used) == (2, 1)
    assert s.can_fit(2, 1, True) and not s.can_fit(3, 0, False)
    s.release(j)
    assert (s.cpu_used, s.gpu_used) == (0, 0)


def test_gpu_gating():
    s = Slots(cpu_total=8, gpu_total=0)
    assert not s.can_fit(1, 1, True)
    assert s.can_fit(1, 0, False)


def test_release_never_negative():
    s = Slots(4, 1)
    j = job()
    s.release(j)
    assert s.cpu_used == 0 and s.gpu_used == 0