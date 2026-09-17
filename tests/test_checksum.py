"""Compare diagnostic CRC composition with independent sequential zlib calls."""
import hashlib
from fractions import Fraction
import random
from types import SimpleNamespace
import zlib

import pytest

from assglass.checksum import WeightStreamChecksum, _apply, _crc_shift_columns


def frame(data, token=None):
    return SimpleNamespace(buffer=memoryview(data), content_token=token)


def test_crc_known_vector_and_empty_stream():
    checksum = WeightStreamChecksum()
    assert checksum.hexdigest() == '00000000'
    checksum.update(frame(b'123456789'))
    assert checksum.hexdigest() == 'cbf43926'
    assert checksum.manifest() == dict(algorithm='crc32', value='cbf43926', frames=1, bytes=9)


def test_varied_sizes_repeated_frames_and_order_match_raw_stream():
    rng = random.Random(8719)
    chunks = [bytes(rng.getrandbits(8) for _ in range(size))
              for size in (0, 1, 4, 19, 96, 2048, 31104)]
    owners = [frame(chunk, object()) for chunk in chunks]
    checksum = WeightStreamChecksum()
    reference = 0
    sequence = [0, 1, 1, 6, 6, 6, 0, 4, 2, 3, 5, 5, 1, 2]
    for index in sequence:
        checksum.update(owners[index])
        reference = zlib.crc32(chunks[index], reference)
        assert checksum.hexdigest() == '%08x' % reference
    assert checksum.frames == len(sequence)
    assert checksum.bytes == sum(len(chunks[index]) for index in sequence)
    assert checksum.reused_frames == 4
    assert checksum.scanned_bytes < checksum.bytes


def test_cached_checksum_does_not_scan_same_immutable_owner_again(monkeypatch):
    checksum = WeightStreamChecksum()
    token = object()
    checksum.update(frame(b'payload', token))
    def forbidden(*_):
        raise AssertionError('cached frame should not be rescanned')
    monkeypatch.setattr('assglass.checksum.zlib.crc32', forbidden)
    # New per-frame metadata objects share an immutable content token.
    for _ in range(20):
        checksum.update(frame(b'payload', token))
    assert checksum.metrics == {'scanned_bytes': 7, 'reused_frames': 20}
    assert checksum.bytes == 21 * 7


def test_missing_token_and_changed_length_never_reuse_wrong_crc():
    checksum = WeightStreamChecksum()
    token = object()
    chunks = [(b'a', None), (b'b', None), (b'c', token), (b'longer', token)]
    for data, identity in chunks:
        checksum.update(frame(data, identity))
    assert checksum.hexdigest() == '%08x' % zlib.crc32(b'abclonger')
    assert checksum.reused_frames == 0


def test_mutable_third_party_weight_frame_is_never_cached_implicitly():
    from assglass.weights import WeightFrame
    data = bytearray(b'first')
    item = WeightFrame(data, (), 0, 0, Fraction(1, 60), 'test')
    checksum = WeightStreamChecksum()
    checksum.update(item)
    data[:] = b'other'
    checksum.update(item)
    assert checksum.hexdigest() == '%08x' % zlib.crc32(b'firstother')
    assert checksum.reused_frames == 0


def test_crc_operator_handles_stream_lengths_beyond_uint32():
    # Shifting by a+b bytes equals two shifts, without allocating multi-GB data.
    a, b = 2**32 + 17, 2**34 + 53
    value = 0xf09a7301
    first = _apply(_crc_shift_columns(a), value)
    assert _apply(_crc_shift_columns(b), first) == _apply(_crc_shift_columns(a + b), value)
    assert _apply(_crc_shift_columns(0), value) == value


def test_sha256_compatibility_scans_all_bytes_even_for_cached_frames():
    checksum = WeightStreamChecksum('sha256')
    item = frame(b'repeated', object())
    for _ in range(10):
        checksum.update(item)
    assert checksum.hexdigest() == hashlib.sha256(b'repeated' * 10).hexdigest()
    assert checksum.scanned_bytes == checksum.bytes == 80
    assert checksum.reused_frames == 0
    assert checksum.manifest()['algorithm'] == 'sha256'


def test_invalid_checksum_algorithm():
    with pytest.raises(ValueError, match='crc32 or sha256'):
        WeightStreamChecksum('md5')
