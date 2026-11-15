"""SSRF-политика для пользовательских URL.

Важно: это первый рубеж. DNS rebinding и редиректы на CDN полностью закрываются
только egress-proxy / NetworkPolicy.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from mf_core.config import Settings, get_settings
from mf_core.errors import ConfigInvalidError

ALLOWED_SCHEMES = {"http", "https", "rtmp", "rtmps", "rtsp", "srt", "mms", "mmsh"}


def check_url(url: str, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    u = urlsplit(url)
    if u.scheme.lower() not in ALLOWED_SCHEMES:
        raise ConfigInvalidError(f"схема {u.scheme!r} запрещена для внешних источников")
    host = u.hostname
    if not host:
        raise ConfigInvalidError("URL без хоста")

    allow = [d.lower().lstrip(".") for d in s.egress_allow_domains]
    if allow and not any(host.lower() == d or host.lower().endswith("." + d) for d in allow):
        raise ConfigInvalidError(f"домен {host} не в allowlist")

    if not s.egress_block_private_ip:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ConfigInvalidError(f"DNS не разрешил {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ConfigInvalidError(f"URL ведёт на внутренний адрес {ip}")