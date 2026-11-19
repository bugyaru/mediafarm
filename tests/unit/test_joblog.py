"""Полные логи инструментов: нормализация, маскирование секретов, буфер LogSink."""
from __future__ import annotations

import uuid

from mf_core import joblog
from mf_core.config import Settings
from worker.logsink import LogSink


def test_normalize_ytdlp():
    assert joblog.normalize("ytdlp", "stdout", "MF|1|2|3|4|id")[0] == "progress"
    assert joblog.normalize("ytdlp", "stdout", "[download]  12.3% of 10MiB")[0] == "progress"
    assert joblog.normalize("ytdlp", "stderr", "ERROR: Video unavailable")[1] == "error"
    assert joblog.normalize("ytdlp", "stdout", "WARNING: no format")[1] == "warning"
    assert joblog.normalize("ytdlp", "stdout", "[debug] System config")[1] == "debug"


def test_normalize_ffmpeg_and_probe():
    assert joblog.normalize("ffmpeg", "stdout", "frame=42")[0] == "progress"
    assert joblog.normalize("ffmpeg", "stderr", "[warning] DTS out of order")[1] == "warning"
    assert joblog.normalize("ffmpeg", "stderr", "[error] boom")[1] == "error"
    assert joblog.normalize("ffprobe", "stderr", "[fatal] no file")[1] == "error"
    assert joblog.normalize("worker", "meta", "шаг 0: acquire")[0] == "meta"


def test_sanitize_masks_secrets_and_ansi():
    out = joblog.sanitize("https://user:pass@host/v?token=abc&x=1")
    assert "pass" not in out and "abc" not in out and "x=1" in out
    assert joblog.sanitize("Authorization: Bearer abcdef").endswith("***")
    assert joblog.sanitize("--password hunter2").endswith("***")
    assert joblog.sanitize("\x1b[31mERROR\x1b[0m: x") == "ERROR: x"
    assert "обрезано" in joblog.sanitize("x" * 100, max_chars=10)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return _Tx()

    async def execute(self, stmt, params=None):
        self.rows.extend(params or [])


class _SF:
    def __init__(self):
        self.rows: list[dict] = []

    def __call__(self):
        return _Session(self.rows)


async def test_logsink_flush_cap_and_files(tmp_path):
    sf = _SF()
    s = Settings(log_stream=True, log_flush_lines=100, log_flush_interval_s=0.05,
                 log_max_lines_per_job=5)
    sink = LogSink(job_id=uuid.uuid4(), attempt=1, session_factory=sf, settings=s,
                   workdir=tmp_path)
    await sink.start()
    sink.set_step(0, "acquire")
    write = sink.writer("ytdlp")
    for i in range(10):
        write("stderr", f"line {i}")
    write("stdout", "MF|1|2|3|4|id")
    await sink.flush()
    await sink.close()
    await sink.close()                      # идемпотентно

    assert sink.total == 6 and sink.dropped == 7 and sink.failed == 0
    assert len(sf.rows) == 7                # meta шага + 4 строки + лимит + итог
    assert all(r["tool"] in joblog.TOOLS for r in sf.rows)
    assert (tmp_path / "00_ytdlp.log").exists()
    assert len((tmp_path / "00_ytdlp.log").read_text().splitlines()) == 11


async def test_logsink_disabled_writes_only_files(tmp_path):
    sf = _SF()
    sink = LogSink(job_id=uuid.uuid4(), attempt=1, session_factory=sf,
                   settings=Settings(log_stream=False), workdir=tmp_path)
    await sink.start()
    sink.set_step(1, "transcode")
    sink.writer("ffmpeg")("stderr", "[error] nvenc")
    await sink.close()
    assert sf.rows == []
    assert "[error] nvenc" in (tmp_path / "01_ffmpeg.log").read_text()