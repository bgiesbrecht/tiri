"""SQLiteStoreProvider — local SQLite KV store. Uses :memory: in tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import Any

from tiri.providers.base import StoreProvider, StoreProviderError


class SQLiteStoreProvider(StoreProvider):
    """Single-connection SQLite store.

    `check_same_thread=False` is required because pytest-asyncio runs each
    test on its own worker thread; a per-thread connection would prevent
    persistence across awaits within one test. A re-entrant lock guards the
    connection.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        self._conn.commit()

    async def get(self, key: str) -> dict | None:
        return await asyncio.to_thread(self._get_sync, key)

    def _get_sync(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv_store WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as e:
            raise StoreProviderError(
                f"Corrupt value at key {key!r}: not JSON ({e})"
            ) from e

    async def put(self, key: str, value: dict) -> None:
        await asyncio.to_thread(self._put_sync, key, value)

    def _put_sync(self, key: str, value: dict) -> None:
        payload = json.dumps(value)
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv_store (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=datetime('now')",
                (key, payload),
            )
            self._conn.commit()

    async def list_keys(self, prefix: str) -> list[str]:
        return await asyncio.to_thread(self._list_keys_sync, prefix)

    def _list_keys_sync(self, prefix: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM kv_store WHERE key LIKE ? ORDER BY key",
                (prefix + "%",),
            ).fetchall()
        return [r[0] for r in rows]

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete_sync, key)

    def _delete_sync(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
