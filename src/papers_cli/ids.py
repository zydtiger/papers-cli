from __future__ import annotations

import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_timestamp_ms = 0


def uuid7() -> str:
    """Return an RFC 9562 UUIDv7-compatible identifier on Python 3.12+."""
    global _last_timestamp_ms
    with _lock:
        timestamp_ms = int(time.time_ns() // 1_000_000)
        # Preserve sort order if the system clock steps backwards within a process.
        timestamp_ms = max(timestamp_ms, _last_timestamp_ms)
        _last_timestamp_ms = timestamp_ms
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))
