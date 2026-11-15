"""Разрешение ссылок на секреты. В БД хранится только secret_ref.

Форматы:
  env:in_smb            -> MF_SECRET_IN_SMB_USERNAME / _PASSWORD / _DOMAIN / ...
  file:/run/secrets/smb -> файлы username, password, ... (K8s Secret volume)
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path

from mf_core.config import get_settings
from mf_core.errors import ConfigInvalidError

_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_LOCK = threading.Lock()


def _from_env(name: str) -> dict[str, str]:
    prefix = f"MF_SECRET_{name.upper()}_"
    out = {k[len(prefix):].lower(): v for k, v in os.environ.items() if k.startswith(prefix)}
    if not out:
        raise ConfigInvalidError(f"секрет env:{name} не найден (ожидались {prefix}*)")
    return out


def _from_file(path: str) -> dict[str, str]:
    root = Path(path)
    if not root.is_dir():
        raise ConfigInvalidError(f"секрет file:{path} — директория не найдена")
    out: dict[str, str] = {}
    for entry in sorted(root.iterdir()):
        if entry.name.startswith(".."):        # k8s atomic writer: ..data, ..2024_...
            continue
        if entry.is_file():
            out[entry.name.lower()] = entry.read_text(encoding="utf-8").strip()
    if not out:
        raise ConfigInvalidError(f"секрет file:{path} пуст")
    return out


class SecretResolver:
    def resolve(self, ref: str | None) -> dict[str, str]:
        if not ref:
            return {}
        ttl = get_settings().secret_cache_ttl_s
        now = time.monotonic()
        with _LOCK:
            hit = _CACHE.get(ref)
            if hit and now - hit[0] < ttl:
                return dict(hit[1])
        kind, _, target = ref.partition(":")
        if kind == "env":
            data = _from_env(target)
        elif kind == "file":
            data = _from_file(target)
        else:
            raise ConfigInvalidError(f"неизвестный тип секрета: {ref!r} (env:|file:)")
        with _LOCK:
            _CACHE[ref] = (now, data)
        return dict(data)

    def fingerprint(self, ref: str | None) -> str:
        """Версия секрета — для ключей кэша backend'ов."""
        data = self.resolve(ref)
        blob = "|".join(f"{k}={v}" for k, v in sorted(data.items()))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @staticmethod
    def values(ref: str | None) -> list[str]:
        """Значения для маскирования в логах/cmdline."""
        try:
            return [v for v in SecretResolver().resolve(ref).values() if len(v) >= 4]
        except Exception:
            return []


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()