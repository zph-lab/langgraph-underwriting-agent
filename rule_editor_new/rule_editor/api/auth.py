"""临时登录系统（2026-09-30）：账号读 config.yaml login 段。

会话为进程内存 token 集合（HttpOnly Cookie 承载），不落地存储；
服务重启后所有会话失效，需重新登录。login.enabled=false 时整体放行。
"""
from __future__ import annotations

import secrets

from ..config import get

COOKIE = "re_session"

_sessions: set[str] = set()


def enabled() -> bool:
    return bool(get("login", "enabled", False))


def verify(username: str, password: str) -> bool:
    u = str(get("login", "username", "") or "")
    p = str(get("login", "password", "") or "")
    if not u and not p:          # 未配置账号：拒绝所有登录，避免空口令直通
        return False
    return (secrets.compare_digest(str(username or ""), u)
            and secrets.compare_digest(str(password or ""), p))


def issue() -> str:
    t = secrets.token_urlsafe(24)
    _sessions.add(t)
    return t


def valid(token: str | None) -> bool:
    return bool(token) and token in _sessions


def discard(token: str | None) -> None:
    _sessions.discard(token or "")
