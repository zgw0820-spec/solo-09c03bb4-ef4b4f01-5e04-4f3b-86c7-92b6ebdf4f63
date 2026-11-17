"""SQLite persistence.

Each role has its own database file. A publish/sync is committed as one
transaction: the new immutable version row and the pointer to the active
serial move together, so an answer can never be assembled from two
versions. A stale transfer (serial <= active serial) is rejected, which
guarantees a late/old transfer can never roll back an already active
version.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import settings

_UTC = timezone.utc
_init_lock = threading.Lock()


def utcnow_iso() -> str:
    return datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        with _init_lock:
            self._init_schema()

    # -- schema ---------------------------------------------------------
    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS zone_versions (
                    serial       INTEGER PRIMARY KEY,
                    records_json TEXT    NOT NULL,
                    record_count INTEGER NOT NULL,
                    published_at TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            row = conn.execute(
                "SELECT value FROM state WHERE key='active_serial'"
            ).fetchone()
            if row is None:
                # Empty seed version: the service answers NXDOMAIN but is
                # internally consistent (one active version) before publish.
                conn.execute(
                    "INSERT OR IGNORE INTO zone_versions "
                    "(serial, records_json, record_count, published_at) "
                    "VALUES (0, '[]', 0, ?)",
                    (utcnow_iso(),),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO state(key, value) "
                    "VALUES ('active_serial', '0')"
                )
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    # -- reads ----------------------------------------------------------
    @staticmethod
    def _get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else row["value"]

    def get_active_version(
        self,
    ) -> Tuple[int, List[Dict[str, str]], Optional[str]]:
        """Return (serial, records, published_at) of one active version."""
        conn = self._connect()
        try:
            serial = int(self._get_state(conn, "active_serial") or "0")
            row = conn.execute(
                "SELECT records_json, published_at FROM zone_versions "
                "WHERE serial=?",
                (serial,),
            ).fetchone()
            records = json.loads(row["records_json"]) if row else []
            return serial, records, (row["published_at"] if row else None)
        finally:
            conn.close()

    def active_serial(self) -> int:
        serial, _, _ = self.get_active_version()
        return serial

    def get_sync_state(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            def g(key: str) -> Optional[str]:
                return self._get_state(conn, key)

            return {
                "active_serial": int(g("active_serial") or "0"),
                "last_sync_at": g("last_sync_at"),
                "last_sync_status": g("last_sync_status") or "never",
                "last_sync_error": g("last_sync_error"),
                "upstream_serial": (
                    int(g("upstream_serial"))
                    if g("upstream_serial") is not None
                    else None
                ),
            }
        finally:
            conn.close()

    def version_exists(self, serial: int) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM zone_versions WHERE serial=?", (serial,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # -- writes ---------------------------------------------------------
    def commit_version(
        self,
        serial: int,
        records: List[Dict[str, str]],
        published_at: str,
    ) -> bool:
        """Atomically store a version and make it the active one.

        Returns True when applied, False when rejected as a stale rollback.
        """
        if serial < 1:
            raise ValueError("serial must be a positive integer")
        payload = json.dumps(records, separators=(",", ":"), sort_keys=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                active = int(
                    self._get_state(conn, "active_serial") or "0"
                )
                if serial <= active:
                    # Late/old transfer or duplicate: never roll back.
                    conn.execute("COMMIT")
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO zone_versions "
                    "(serial, records_json, record_count, published_at) "
                    "VALUES (?, ?, ?, ?)",
                    (serial, payload, len(records), published_at),
                )
                conn.execute(
                    "UPDATE state SET value=? WHERE key='active_serial'",
                    (str(serial),),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def next_publish(
        self, records: List[Dict[str, str]]
    ) -> Tuple[int, str]:
        """Primary only: increment serial only after a valid publish."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                next_serial = int(
                    self._get_state(conn, "active_serial") or "0"
                ) + 1
                published_at = utcnow_iso()
                conn.execute(
                    "INSERT INTO zone_versions "
                    "(serial, records_json, record_count, published_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        next_serial,
                        json.dumps(
                            records, separators=(",", ":"), sort_keys=True
                        ),
                        len(records),
                        published_at,
                    ),
                )
                conn.execute(
                    "UPDATE state SET value=? WHERE key='active_serial'",
                    (str(next_serial),),
                )
                conn.execute("COMMIT")
                return next_serial, published_at
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def update_sync_state(
        self,
        status: str,
        error: Optional[str],
        upstream_serial: Optional[int],
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO state(key, value) VALUES('last_sync_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (utcnow_iso(),),
                )
                conn.execute(
                    "INSERT INTO state(key, value) "
                    "VALUES('last_sync_status', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (status,),
                )
                conn.execute(
                    "INSERT INTO state(key, value) "
                    "VALUES('last_sync_error', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (error or "",),
                )
                if upstream_serial is not None:
                    conn.execute(
                        "INSERT INTO state(key, value) "
                        "VALUES('upstream_serial', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(upstream_serial),),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()


storage = Storage(settings.db_path)
