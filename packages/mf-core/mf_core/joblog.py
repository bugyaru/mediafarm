"""Полные построчные логи инструментов задачи (yt-dlp/ffmpeg/ffmbc/ffprobe/worker).

Хранятся в таблице job_log, пишутся воркером пачками, читаются UI по курсору id.
NOTIFY job_logs (триггер из миграции 0005) будит WebSocket-мост UI.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TOOLS = ("ytdlp", "ffmpeg", "ffmbc", "ffprobe", "worker")
STREAMS = ("stdout", "stderr", "progress", "meta")
LEVELS = ("debug", "info", "warning", "error")
MAX_LINE = 8000

# ------------------------------ очистка строк -------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_URL_CRED = re.compile(r"(://[^:/\s]+:)[^@\s]+(@)")
_QSECRET = re.compile(
    r"(?i)\b(token|access_token|id_token|auth|authorization|signature|sig|api_key|apikey|"
    r"password|passwd|pwd|secret|sessionid|jsessionid|cookie)=([^&\s\"']+)"
)
_HEADER = re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key)(\s*:\s*).+$")
_CLI_SECRET = re.compile(r"(?i)(--(?:password|token|api-key|secret)(?:=|\s+))\S+")

_FF_LEVEL = re.compile(r"^\s*\[(fatal|error|warning|info|verbose|debug)\]", re.IGNORECASE)
_YT_LEVEL = re.compile(r"^\s*(ERROR|WARNING|DEBUG):", re.IGNORECASE)
_YT_PROGRESS = re.compile(r"^\[download\]\s+\d")
_LEVEL_MAP = {"fatal": "error", "error": "error", "warning": "warning",
              "info": "info", "verbose": "debug", "debug": "debug"}


def sanitize(line: str, *, max_chars: int = MAX_LINE, redact: bool = True) -> str:
    """Убирает ANSI, маскирует креды/токены/cookie, ограничивает длину строки."""
    line = _ANSI.sub("", line).rstrip("\r\n")
    if redact:
        line = _URL_CRED.sub(r"\1***\2", line)
        line = _QSECRET.sub(r"\1=***", line)
        line = _HEADER.sub(r"\1\2***", line)
        line = _CLI_SECRET.sub(r"\1***", line)
    if max_chars and len(line) > max_chars:
        line = line[:max_chars] + f"… [обрезано {len(line) - max_chars} симв.]"
    return line


def normalize(
        tool: str, stream: str, line: str, *, max_chars: int = MAX_LINE, redact: bool = True,
) -> tuple[str, str, str]:
    """(stream, level, text) — приводит сырую строку инструмента к записи job_log."""
    text_ = sanitize(line, max_chars=max_chars, redact=redact)
    low = text_.lstrip()
    kind = stream if stream in STREAMS else "stdout"

    if tool == "ytdlp":
        if text_.startswith("MF|") or _YT_PROGRESS.match(low):
            kind = "progress"
    elif tool in ("ffmpeg", "ffmbc") and stream == "stdout":
        kind = "progress"          # -progress pipe:1 → служебные key=value

    level = "info"
    if kind == "progress":
        level = "debug"
    elif m := _FF_LEVEL.match(low):
        level = _LEVEL_MAP.get(m.group(1).lower(), "info")
    elif m := _YT_LEVEL.match(low):
        level = _LEVEL_MAP.get(m.group(1).lower(), "info")
    elif low.startswith("[debug]"):
        level = "debug"
    elif kind == "meta":
        level = "info"
    if kind == "stderr" and level == "info" and re.search(r"(?i)\b(error|failed|fatal)\b", text_):
        level = "error"
    return kind, level, text_


def row(
        job_id: uuid.UUID | str, *, step_idx: int, attempt: int, tool: str,
        stream: str, level: str, line: str, ts: dt.datetime | None = None,
) -> dict[str, Any]:
    return {"job_id": str(job_id), "step_idx": step_idx, "attempt": attempt, "tool": tool,
            "stream": stream, "level": level, "line": line, "ts": ts or dt.datetime.now(dt.UTC)}


# ---------------------------------- запись ----------------------------------
INSERT = text("""
INSERT INTO job_log (job_id, step_idx, attempt, ts, tool, stream, level, line)
VALUES (CAST(:job_id AS uuid), :step_idx, :attempt, :ts, :tool, :stream, :level, :line)
""")


async def append(session: AsyncSession, rows: Sequence[dict]) -> int:
    """Пачечная вставка (executemany). Один INSERT = один NOTIFY на задачу."""
    if not rows:
        return 0
    for chunk_start in range(0, len(rows), 500):
        await session.execute(INSERT, list(rows[chunk_start:chunk_start + 500]))
    return len(rows)


# ---------------------------------- чтение ----------------------------------
def _filters(
        job_id: uuid.UUID | str, *, step: int | None = None, tool: str | None = None,
        streams: Sequence[str] | None = None, search: str | None = None,
        level: str | None = None, attempt: int | None = None,
) -> tuple[str, dict[str, Any]]:
    where = ["job_id = CAST(:jid AS uuid)"]
    args: dict[str, Any] = {"jid": str(job_id)}
    if step is not None:
        where.append("step_idx = :step")
        args["step"] = step
    if tool:
        where.append("tool = :tool")
        args["tool"] = tool
    if streams:
        keys = []
        for i, sv in enumerate(streams):
            key = f"stream{i}"
            keys.append(f":{key}")
            args[key] = sv
        where.append(f"stream IN ({', '.join(keys)})")
    if level:
        where.append("level = :level")
        args["level"] = level
    if attempt is not None:
        where.append("attempt = :attempt")
        args["attempt"] = attempt
    if search:
        where.append("line ILIKE :search")
        args["search"] = f"%{search}%"
    return " AND ".join(where), args


def _out(r: Any) -> dict[str, Any]:
    return {"id": r["id"], "ts": r["ts"].isoformat() if r["ts"] else None,
            "step_idx": r["step_idx"], "attempt": r["attempt"], "tool": r["tool"],
            "stream": r["stream"], "level": r["level"], "line": r["line"]}


async def fetch(
        session: AsyncSession, job_id: uuid.UUID | str, *, after: int = 0, before: int | None = None,
        limit: int = 500, desc: bool = False, **flt: Any,
) -> list[dict[str, Any]]:
    where, args = _filters(job_id, **flt)
    if after:
        where += " AND id > :after"
        args["after"] = after
    if before:
        where += " AND id < :before"
        args["before"] = before
    args["limit"] = max(1, min(int(limit), 5000))
    order = "DESC" if desc else "ASC"
    sql = f"SELECT * FROM job_log WHERE {where} ORDER BY id {order} LIMIT :limit"
    rows = (await session.execute(text(sql), args)).mappings().all()
    out = [_out(r) for r in rows]
    return list(reversed(out)) if desc else out


async def tail(
        session: AsyncSession, job_id: uuid.UUID | str, *, limit: int = 500, **flt: Any,
) -> list[dict[str, Any]]:
    """Последние N строк в хронологическом порядке — стартовый кадр для UI."""
    return await fetch(session, job_id, limit=limit, desc=True, **flt)


async def summary(session: AsyncSession, job_id: uuid.UUID | str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT step_idx, tool, count(*) AS lines,
               count(*) FILTER (WHERE level = 'error') AS errors,
               count(*) FILTER (WHERE level = 'warning') AS warnings,
               min(id) AS first_id, max(id) AS last_id, min(ts) AS first_ts, max(ts) AS last_ts
        FROM job_log WHERE job_id = CAST(:jid AS uuid)
        GROUP BY step_idx, tool ORDER BY step_idx, tool
    """), {"jid": str(job_id)})).mappings().all()
    return [dict(r) for r in rows]


async def totals(session: AsyncSession, job_id: uuid.UUID | str) -> dict[str, Any]:
    r = (await session.execute(text("""
        SELECT count(*) AS lines, COALESCE(max(id), 0) AS last_id,
               count(*) FILTER (WHERE level = 'error') AS errors
        FROM job_log WHERE job_id = CAST(:jid AS uuid)
    """), {"jid": str(job_id)})).mappings().first()
    return {"lines": int(r["lines"]), "last_id": int(r["last_id"]), "errors": int(r["errors"])}


async def purge(session: AsyncSession, days: int) -> int:
    res = await session.execute(
        text("DELETE FROM job_log WHERE ts < now() - make_interval(days => :d)"), {"d": days})
    return res.rowcount or 0