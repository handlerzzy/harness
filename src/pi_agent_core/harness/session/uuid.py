from __future__ import annotations

import secrets
import threading
import time

_last_timestamp = -(2**53)
_sequence = 0
_lock = threading.Lock()


def uuidv7() -> str:
    global _last_timestamp, _sequence
    random = secrets.token_bytes(16)
    timestamp = int(time.time() * 1000)
    with _lock:
        if timestamp > _last_timestamp:
            _sequence = int.from_bytes(random[6:10], "big")
            _last_timestamp = timestamp
        else:
            _sequence = (_sequence + 1) & 0xFFFFFFFF
            if _sequence == 0:
                _last_timestamp += 1
        current_timestamp = _last_timestamp
        sequence = _sequence

    data = bytearray(16)
    data[:6] = current_timestamp.to_bytes(6, "big", signed=False)
    data[6] = 0x70 | ((sequence >> 28) & 0x0F)
    data[7] = (sequence >> 20) & 0xFF
    data[8] = 0x80 | ((sequence >> 14) & 0x3F)
    data[9] = (sequence >> 6) & 0xFF
    data[10] = ((sequence & 0x3F) << 2) | (random[10] & 0x03)
    data[11:] = random[11:]
    value = data.hex()
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"
