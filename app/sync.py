"""Backup-side zone synchronisation.

The backup pulls the *complete* zone from the primary over HTTP and
applies it atomically. Properties implemented here:

* a transfer that fails (HTTP error, timeout, broken connection, invalid
  payload) never touches the active version — the old one keeps serving;
* a stale transfer whose serial is not newer than the active version is
  rejected, so a late/old transfer can never roll back the zone;
* a background loop plus a startup pull re-sync automatically after a
  disconnect or a process restart, eventually catching up to the newest
  version ("reconcile until current").
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx

from .config import settings
from .storage import storage

log = logging.getLogger("sync")


class SyncOutcome:
    def __init__(
        self,
        status: str,
        applied: bool,
        local_serial: int,
        upstream_serial: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        self.status = status
        self.applied = applied
        self.local_serial = local_serial
        self.upstream_serial = upstream_serial
        self.error = error

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "applied": self.applied,
            "local_serial": self.local_serial,
            "upstream_serial": self.upstream_serial,
            "error": self.error,
        }


async def sync_once(client: Optional[httpx.AsyncClient] = None) -> SyncOutcome:
    """One pull-and-apply cycle. Never raises: failures keep the old zone."""
    local_serial = storage.active_serial()
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=settings.http_timeout)
    try:
        try:
            resp = await client.get(
                f"{settings.primary_api_url.rstrip('/')}/api/zone"
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # network / HTTP / decode failure
            err = f"{type(exc).__name__}: {exc}"
            storage.update_sync_state("failed", err, None)
            log.warning("sync transfer failed, keeping serial=%s: %s",
                        local_serial, err)
            return SyncOutcome("failed", False, local_serial, None, err)

        upstream_serial = int(payload.get("serial", -1))
        records = payload.get("records")
        published_at = payload.get("published_at")
        if (
            upstream_serial < 1
            or not isinstance(records, list)
            or not isinstance(published_at, str)
            or any(
                not isinstance(r, dict)
                or not isinstance(r.get("name"), str)
                or not isinstance(r.get("ip"), str)
                for r in records
            )
        ):
            err = "malformed zone payload from primary"
            storage.update_sync_state("failed", err, upstream_serial)
            return SyncOutcome(
                "failed", False, local_serial, upstream_serial, err
            )

        if upstream_serial <= local_serial:
            # Late/old transfer: must never roll back an active version.
            storage.update_sync_state(
                "ignored_stale", None, upstream_serial
            )
            log.info(
                "ignored stale transfer upstream=%s local=%s",
                upstream_serial, local_serial,
            )
            return SyncOutcome(
                "ignored_stale", False, local_serial, upstream_serial
            )

        # Apply the complete new version in one atomic transaction.
        applied = storage.commit_version(
            upstream_serial,
            [{"name": r["name"], "ip": r["ip"]} for r in records],
            published_at,
        )
        if applied:
            storage.update_sync_state("synced", None, upstream_serial)
            log.info("applied serial=%s from primary", upstream_serial)
            return SyncOutcome(
                "synced", True, upstream_serial, upstream_serial
            )
        # Lost a race against another sync (e.g. manual + background).
        storage.update_sync_state(
            "ignored_stale", None, upstream_serial
        )
        return SyncOutcome(
            "ignored_stale", False, storage.active_serial(), upstream_serial
        )
    finally:
        if own:
            await client.aclose()


class SyncLoop:
    """Background reconcile loop: keep catching up until current."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._run_lock = asyncio.Lock()

    async def start(self) -> None:
        # Catch up immediately after (re)start, then on an interval.
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="sync-loop")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.trigger()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("sync loop error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.sync_interval
                )
            except asyncio.TimeoutError:
                pass

    async def trigger(self) -> SyncOutcome:
        """Manual and automatic syncs share one lock to avoid races."""
        async with self._run_lock:
            async with httpx.AsyncClient(
                timeout=settings.http_timeout
            ) as client:
                return await sync_once(client)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


sync_loop = SyncLoop()
