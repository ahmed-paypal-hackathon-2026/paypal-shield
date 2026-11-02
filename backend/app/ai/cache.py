"""Async LRU pillar-score cache keyed by SHA-256 image hash (Person 2).

Thread/task-safe via asyncio.Lock. Capacity defaults to 512 entries.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from typing import Any, Optional


class PillarCache:
    """Simple async LRU cache backed by an OrderedDict."""

    def __init__(self, capacity: int = 512, maxsize: Optional[int] = None) -> None:
        """Initialise the cache.

        Args:
            capacity: Maximum number of entries (default 512).
            maxsize:  Alias for `capacity` (accepted for API compatibility).
        """
        effective_capacity = maxsize if maxsize is not None else capacity
        if effective_capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = effective_capacity
        self._store: OrderedDict[str, Any] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Return the cached value for *key*, or None if not present."""
        async with self._lock:
            if key not in self._store:
                return None
            # Move to end (most-recently-used)
            self._store.move_to_end(key)
            return self._store[key]

    async def set(self, key: str, value: Any) -> None:
        """Store *value* under *key*, evicting the LRU entry if at capacity."""
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
                return
            if len(self._store) >= self._capacity:
                self._store.popitem(last=False)  # remove LRU (first item)
            self._store[key] = value

    async def delete(self, key: str) -> bool:
        """Remove *key* from the cache. Returns True if it was present."""
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def clear(self) -> None:
        """Evict all entries."""
        async with self._lock:
            self._store.clear()

    def size(self) -> int:
        """Return the current number of cached entries (non-blocking snapshot)."""
        return len(self._store)

    @property
    def capacity(self) -> int:
        return self._capacity


def image_hash(image_bytes: bytes) -> str:
    """Return the hex-encoded SHA-256 digest of *image_bytes*."""
    return hashlib.sha256(image_bytes).hexdigest()


# Module-level singleton — import this directly in other AI modules.
pillar_cache: PillarCache = PillarCache(capacity=512)
