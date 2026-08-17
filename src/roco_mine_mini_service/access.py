"""Internal day-scoped access used by the single-user game connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class AccessGrant:
    uin: str
    expires_at: datetime


class AccessProvider:
    """Keep the currently scanned account valid until the daily cutoff."""

    def __init__(self) -> None:
        self._grant: AccessGrant | None = None
        self._lock = Lock()

    def issue(self, uin: str, expires_at: datetime) -> AccessGrant:
        grant = AccessGrant(uin=str(uin), expires_at=expires_at)
        with self._lock:
            self._grant = grant
        return grant

    def current_grant(self, uin: str) -> AccessGrant | None:
        with self._lock:
            grant = self._grant
        if grant is None or grant.uin != str(uin):
            return None
        if datetime.now(SHANGHAI) >= grant.expires_at:
            return None
        return grant

    def clear(self) -> None:
        with self._lock:
            self._grant = None
