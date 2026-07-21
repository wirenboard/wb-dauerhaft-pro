"""
Codec unit tests for the cases requested in review: a frame of the correct
length parses, and each wrong-length/corruption mode gets its own diagnostic.
"""

import logging

import pytest

from wb.dauerhaft_pro import protocol

# A reply captured from the live actuator on the test stand (roller 0x5F).
VALID_REPLY = bytes.fromhex("5f0102015f5199")


def test_valid_frame_parses():
    """
    A correct-length frame captured from a live actuator parses and decodes.
    """
    frame = protocol.parse_frame(VALID_REPLY)
    assert (frame.address, frame.function, frame.data) == (0x5F, protocol.Function.QUERY, b"\x01\x5f")
    resp = protocol.decode_response(frame)
    assert isinstance(resp, protocol.QueryResponse)
    assert resp.address == 0x5F


@pytest.mark.parametrize(
    "truncated,message",
    [(VALID_REPLY[:4], "too short"), (VALID_REPLY[:-1], "shorter than declared")],
    ids=["below-minimum", "shorter-than-declared"],
)
def test_truncated_frames_raise_frame_error(truncated, message):
    """
    A frame shorter than the minimum, or shorter than its declared length,
    raises FrameError.
    """
    with pytest.raises(protocol.FrameError, match=message):
        protocol.parse_frame(truncated)


def test_frame_longer_than_declared_is_trimmed(caplog):
    """
    Trailing junk after a whole frame is trimmed, with a warning logged.
    """
    # trailing line junk after a complete frame: salvage the declared span
    caplog.set_level(logging.WARNING, logger="wb.dauerhaft_pro.protocol")
    resp = protocol.parse_response(VALID_REPLY + b"\x00\xff")
    assert isinstance(resp, protocol.QueryResponse)
    assert resp.address == 0x5F
    assert "longer than declared" in caplog.text


def test_corrupted_byte_raises_crc_error():
    """
    A corrupted byte raises CrcError.
    """
    corrupted = bytearray(VALID_REPLY)
    corrupted[3] ^= 0x01
    with pytest.raises(protocol.CrcError, match="CRC mismatch"):
        protocol.parse_frame(bytes(corrupted))
