"""Exclusive ownership leases for browser-backed tmux attachments."""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import threading
import time
from typing import Protocol


class Connection(Protocol):
    closed: threading.Event

    def revoke(self, new_ip: str, notify: bool = True) -> None: ...


@dataclass
class Lease:
    name: str
    page: str
    token: str
    ip: str
    since: float = field(default_factory=time.time)
    reserved_at: float = field(default_factory=time.monotonic)
    connection: Connection | None = None

    def public(self) -> dict:
        return {"ip": self.ip, "since": self.since}


class Registry:
    """One current browser lease per tmux session.

    IP is deliberately display-only.  Ownership is determined solely by the
    random page id, random server token, and the bound WebSocket connection.
    """

    RESERVATION_TTL = 15.0

    def __init__(self):
        self._lock = threading.Lock()
        self._leases: dict[str, Lease] = {}

    def _live_locked(self, name: str) -> Lease | None:
        lease = self._leases.get(name)
        if (lease and lease.connection is None
                and time.monotonic() - lease.reserved_at > self.RESERVATION_TTL):
            self._leases.pop(name, None)
            return None
        return lease

    def claim(self, name: str, page: str, ip: str, force: bool = False) -> dict:
        if not page or len(page) > 128:
            return {"error": "页面标识无效"}
        with self._lock:
            old = self._live_locked(name)
            if old and old.page != page and not force:
                return {"conflict": True, "owner": old.public()}
            lease = Lease(name=name, page=page, token=secrets.token_urlsafe(32), ip=ip)
            self._leases[name] = lease

        # Publish the replacement first, then terminate the former connection.
        # Its finally block cannot remove the new lease because tokens differ.
        if old and old.connection:
            old.connection.revoke(ip, notify=old.page != page)
            old.connection.closed.wait(2.0)
        return {"ok": True, "token": lease.token, "owner": lease.public()}

    def bind(self, name: str, page: str, token: str,
             connection: Connection) -> bool:
        with self._lock:
            lease = self._live_locked(name)
            if not lease or lease.page != page or not secrets.compare_digest(
                    lease.token, token):
                return False
            if lease.connection is not None:
                return False
            lease.connection = connection
            return True

    def release(self, name: str, token: str) -> None:
        with self._lock:
            lease = self._leases.get(name)
            if lease and secrets.compare_digest(lease.token, token):
                self._leases.pop(name, None)

    def owner(self, name: str) -> dict | None:
        with self._lock:
            lease = self._live_locked(name)
            return lease.public() if lease else None
