import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.main import rec_once


class DummySerial:
    def __init__(self, payload: tuple[int, ...], timeout):
        self._timeout = timeout
        self._readline_queue = [b"ACK\n", f"DATA,{len(payload)}\n".encode()]
        self._payload = struct.pack("<" + "h" * len(payload), *payload)
        self._payload_pos = 0
        self.written = []

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self._timeout = value

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data: bytes):
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        if self._readline_queue:
            return self._readline_queue.pop(0)
        return b""

    def readinto(self, mv):
        remaining = len(self._payload) - self._payload_pos
        if remaining <= 0:
            return 0
        count = min(remaining, len(mv))
        mv[:count] = self._payload[self._payload_pos : self._payload_pos + count]
        self._payload_pos += count
        return count


def _run_rec_once_with_timeout(initial_timeout):
    payload = (1, -2, 3, -4)
    ser = DummySerial(payload, timeout=initial_timeout)
    samples = rec_once(ser, sr=4, seconds=1.0)
    return ser, samples, payload


def test_rec_once_restores_timeout_numeric():
    initial_timeout = 1.5
    ser, samples, payload = _run_rec_once_with_timeout(initial_timeout)
    assert ser.timeout == initial_timeout
    np.testing.assert_array_equal(samples, np.array(payload, dtype=np.int16))


def test_rec_once_restores_timeout_none():
    ser, samples, payload = _run_rec_once_with_timeout(None)
    assert ser.timeout is None
    np.testing.assert_array_equal(samples, np.array(payload, dtype=np.int16))
