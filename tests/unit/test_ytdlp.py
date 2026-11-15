import pytest

from mf_core.errors import ConfigInvalidError, SourceInvalidError, TransientNetworkError
from mf_core.media.ytdlp import build_cmd, classify_ytdlp, parse_progress
from worker.egress import check_url


def test_cmd_has_resume_progress_and_filepath():
    cmd = build_cmd("https://x/y", outdir="/tmp/w")
    assert "--continue" in cmd and "--no-simulate" in cmd
    assert "after_move:filepath" in cmd
    assert any(a.startswith("download:MF|") for a in cmd)
    assert "--fragment-retries" in cmd and "--concurrent-fragments" in cmd
    assert cmd[-1] == "https://x/y"


def test_cmd_options():
    cmd = build_cmd("https://x/y", outdir="/w", resume=False, live=True, live_from_start=True,
                    max_duration_s=600, cookies_file="/c.txt", archive_file="/a.txt",
                    format_selector="bv*+ba/b", playlist=True)
    assert "--no-continue" in cmd and "--live-from-start" in cmd
    assert "*0-600" in cmd and "--cookies" in cmd and "--download-archive" in cmd
    assert "-f" in cmd and "--yes-playlist" in cmd


def test_progress_parsing():
    snap = parse_progress("MF|1000|4000|2048.5|30|abc123")
    assert snap["pct"] == 25.0 and snap["speed"] == 2048.5 and snap["id"] == "abc123"
    assert parse_progress("[download] 12%") is None
    assert parse_progress("MF|NA|NA|NA|NA|x")["pct"] is None


def test_error_classification():
    assert isinstance(classify_ytdlp(1, ["HTTP Error 403: Forbidden"]), ConfigInvalidError)
    assert isinstance(classify_ytdlp(1, ["Video unavailable"]), SourceInvalidError)
    assert isinstance(classify_ytdlp(1, ["Connection reset by peer"]), TransientNetworkError)


def test_egress_blocks_private(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    with pytest.raises(ConfigInvalidError):
        check_url("http://localhost/x")
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    check_url("https://example.com/x")


def test_egress_allowlist(monkeypatch):
    import socket

    from mf_core.config import Settings
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    s = Settings(egress_allow_domains=["example.com"])
    check_url("https://cdn.example.com/v", s)
    with pytest.raises(ConfigInvalidError):
        check_url("https://evil.com/v", s)