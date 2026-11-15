"""Таксономия ошибок MediaFarm: определяет retry-политику и fallback на CPU."""
from __future__ import annotations

import random
import re
from typing import Any


class MFError(Exception):
    error_class: str = "unknown"
    code: str = "error"
    retryable: bool = False
    fallback_cpu: bool = False

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {
            "class": self.error_class,
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message[:2000],
            "details": self.details,
        }


class TransientNetworkError(MFError):
    error_class, code, retryable = "transient_network", "network", True


class StallError(MFError):
    error_class, code, retryable = "stall", "no_progress", True


class HWAccelError(MFError):
    error_class, code, retryable, fallback_cpu = "hwaccel", "gpu_unavailable", True, True


class ResourceError(MFError):
    error_class, code, retryable = "resource", "resource_exhausted", True


class SourceInvalidError(MFError):
    error_class, code, retryable = "source_invalid", "invalid_source", False


class SourceNotReadyError(MFError):
    """Файл ещё дописывается / плейлист пуст — отложенная попытка."""
    error_class, code, retryable = "source_invalid", "not_ready", True


class ConfigInvalidError(MFError):
    error_class, code, retryable = "config_invalid", "invalid_config", False


class StorageError(MFError):
    error_class, code, retryable = "transient_network", "storage_io", True


class StorageAuthError(StorageError):
    error_class, code, retryable = "config_invalid", "storage_auth", False


class PublishConflictError(MFError):
    error_class, code, retryable = "config_invalid", "publish_conflict", False


class CancelledByUser(MFError):
    error_class, code, retryable = "cancelled", "cancelled", False


class LeaseLost(MFError):
    error_class, code, retryable = "infra", "lease_lost", True


class VerifyFailed(MFError):
    error_class, code, retryable = "source_invalid", "verify_failed", False


# --- классификация stderr ffmpeg / ffmbc -----------------------------------
_HW_PATTERNS = (
    r"Cannot load (libnvidia|libcuda)", r"No capable devices found",
    r"OpenEncodeSessionEx failed", r"out of memory", r"CUDA_ERROR",
    r"nvenc.*not available", r"Failed setup for format cuda",
    r"Impossible to convert between the formats", r"Error initializing output stream.*nvenc",
    r"Provided device doesn't support", r"no nvenc capable devices",
)
_NET_PATTERNS = (
    r"Connection (reset|refused|timed out)", r"Server returned 4\d\d|Server returned 5\d\d",
    r"HTTP error", r"Temporary failure in name resolution", r"I/O error",
    r"Error in the pull function", r"Failed to resolve hostname", r"Network is unreachable",
)
_SRC_PATTERNS = (
    r"Invalid data found when processing input", r"moov atom not found",
    r"could not find codec parameters", r"Unknown decoder", r"End of file",
    r"DRM protected", r"Unsupported codec",
)
_RES_PATTERNS = (r"No space left on device", r"Cannot allocate memory", r"Too many open files")
_CFG_PATTERNS = (
    r"Unknown encoder", r"Unknown decoder", r"No such filter", r"Unrecognized option",
    r"Invalid argument", r"Error parsing options",
)


def _match(patterns: tuple[str, ...], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return None


def classify_ffmpeg(rc: int, tail: list[str]) -> MFError:
    text = "\n".join(tail[-80:])
    low = text.lower()
    if p := _match(_HW_PATTERNS, text):
        # 'out of memory' на GPU — это hwaccel, на CPU — resource
        if "out of memory" in low and "cuda" not in low and "nvenc" not in low:
            return ResourceError("OOM при кодировании", rc=rc, matched=p, tail=tail[-40:])
        return HWAccelError("аппаратное ускорение недоступно", rc=rc, matched=p, tail=tail[-40:])
    if p := _match(_RES_PATTERNS, text):
        return ResourceError("нехватка ресурсов", rc=rc, matched=p, tail=tail[-40:])
    if p := _match(_NET_PATTERNS, text):
        return TransientNetworkError("сетевая ошибка входа/выхода", rc=rc, matched=p, tail=tail[-40:])
    if p := _match(_SRC_PATTERNS, text):
        return SourceInvalidError("битый или неподдерживаемый источник", rc=rc, matched=p, tail=tail[-40:])
    if p := _match(_CFG_PATTERNS, text):
        return ConfigInvalidError("некорректные аргументы кодека/фильтра", rc=rc, matched=p, tail=tail[-40:])
    return MFError(f"ffmpeg завершился с кодом {rc}", rc=rc, tail=tail[-40:])


def classify(exc: BaseException) -> MFError:
    if isinstance(exc, MFError):
        return exc
    if isinstance(exc, TimeoutError):
        return TransientNetworkError(f"timeout: {exc}")
    if isinstance(exc, ConnectionError):
        return TransientNetworkError(str(exc))
    if isinstance(exc, FileNotFoundError):
        return SourceInvalidError(f"не найдено: {exc}")
    if isinstance(exc, PermissionError):
        return StorageAuthError(f"доступ запрещён: {exc}")
    if isinstance(exc, OSError):
        import errno
        if exc.errno in (errno.ENOSPC, errno.ENOMEM, errno.EMFILE):
            return ResourceError(str(exc))
        if exc.errno in (errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT, errno.EHOSTUNREACH):
            return TransientNetworkError(str(exc))
        return StorageError(str(exc), errno=exc.errno)
    return MFError(f"{type(exc).__name__}: {exc}")


def exp_backoff(attempts: int, base: float = 15.0, cap: float = 600.0) -> float:
    """Экспоненциальная задержка с джиттером ±20%."""
    raw = min(cap, base * (2 ** max(0, attempts - 1)))
    return round(raw * random.uniform(0.8, 1.2), 2)