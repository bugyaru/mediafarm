from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


def asset_identity(storage_id: uuid.UUID | str | None, uri: str, fingerprint: str) -> str:
    """Стабильный ключ дедупликации ассета (уникальный индекс в БД)."""
    blob = f"{storage_id or '-'}|{uri}|{fingerprint}"
    return hashlib.sha256(blob.encode()).hexdigest()


def job_idempotency_key(
        asset_identity_key: str, preset_name: str, preset_version: int, output_uri: str
) -> str:
    blob = f"{asset_identity_key}|{preset_name}|{preset_version}|{output_uri}"
    return hashlib.sha256(blob.encode()).hexdigest()


def mask(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", text)


def parse_size(value: str | int | None) -> int | None:
    """'6M' / '192k' / '1.5G' -> байты (используется в пресетах)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgGtT]?)\s*", value)
    if not m:
        raise ValueError(f"некорректный размер: {value!r}")
    mult = {"": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)