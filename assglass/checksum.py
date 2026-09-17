"""Diagnostic checksums of the exact Y/U/V byte stream sent to FFmpeg.

CRC32 is a non-cryptographic checksum, never a cache equality test. A cached
immutable WeightFrame content token permits reuse of its CRC. Combining those
CRCs still gives zlib.crc32(concatenated_bytes), including repeated frames and
their order, without rereading every cached frame's pixels.
"""
import hashlib
import zlib


def _apply(columns, value):
    result = 0
    bit = 0
    while value:
        if value & 1:
            result ^= columns[bit]
        value >>= 1
        bit += 1
    return result


def _crc_shift_columns(length):
    """Linear CRC register shift by length bytes, in GF(2).

    Use the reflected IEEE polynomial used by zlib, exponentiating the
    one-zero-byte operator. This operates on CRCs (32 bits), not pixel data.
    The caller retains only the operator for the current frame size.
    """
    if length < 0:
        raise ValueError('checksum length must be nonnegative')
    result = tuple(1 << bit for bit in range(32))
    power = []
    for column in result:
        for _ in range(8):
            column = (column >> 1) ^ (0xedb88320 if column & 1 else 0)
        power.append(column)
    while length:
        if length & 1:
            result = tuple(_apply(power, column) for column in result)
        length >>= 1
        if length:
            power = tuple(_apply(power, column) for column in power)
    return result


class WeightStreamChecksum:
    def __init__(self, algorithm='crc32'):
        if algorithm not in ('crc32', 'sha256'):
            raise ValueError('mask hash must be crc32 or sha256')
        self.algorithm = algorithm
        self._sha = hashlib.sha256() if algorithm == 'sha256' else None
        self._crc = 0
        self._token = None
        self._frame_crc = 0
        self._length = None
        self._columns = None
        self.frames = self.bytes = 0
        self.scanned_bytes = self.reused_frames = 0

    def update(self, frame):
        view = frame.buffer
        try:
            size = view.nbytes
            if self._sha is not None:
                self._sha.update(view)
                self.scanned_bytes += size
            else:
                # Only native immutable weights issue reusable content tokens.
                # A missing token never enables reuse for third-party frames.
                token = getattr(frame, 'content_token', None)
                reused = token is not None and token is self._token and size == self._length
                if reused:
                    crc = self._frame_crc
                else:
                    crc = zlib.crc32(view) & 0xffffffff
                    self.scanned_bytes += size
                if self._length != size:
                    self._columns = _crc_shift_columns(size)
                self._crc = _apply(self._columns, self._crc) ^ crc
                self._token, self._frame_crc, self._length = token, crc, size
                self.reused_frames += int(reused)
            self.frames += 1
            self.bytes += size
        finally:
            del view

    def hexdigest(self):
        return self._sha.hexdigest() if self._sha is not None else '%08x' % self._crc

    def manifest(self):
        return {'algorithm': self.algorithm, 'value': self.hexdigest(),
                'frames': self.frames, 'bytes': self.bytes}

    @property
    def metrics(self):
        return {'scanned_bytes': self.scanned_bytes, 'reused_frames': self.reused_frames}
