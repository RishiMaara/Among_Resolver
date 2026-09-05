"""
An upload has a ceiling, and the ceiling is enforced before the memory is spent.

/reconcile/upload read three files per request with a bare `await file.read()`
and no limit anywhere in main.py or file_agent.py. That buffers the whole
upload before anything inspects it, so a large file takes the process down with
an OOM instead of a message saying what was wrong.

It did not need to be an attack. A controller exporting a year of gateway
traffic produces a large file honestly, and "the engine died" is a bad answer
to give them.

The rest of this service's security posture is deliberate — constant-time key
comparison, CORS narrowed with the reasoning written down, a warning at every
startup when auth is off. An unbounded read sitting next to that is the kind of
gap a reviewer reads as inattention rather than as a judgement call.
"""

# asyncio.run rather than pytest-asyncio: six tests do not justify a new
# test-time dependency in a requirements file that is installed in CI.
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import main  # noqa: E402


class FakeUpload:
    """
    An upload that reports how much of it was actually consumed.

    `served` is the point: a cap that reads everything and then checks the
    length has already paid the cost it exists to avoid, and a test that only
    asserts on the raised error cannot tell the two implementations apart.
    """

    def __init__(self, total: int, filename: str = "big.csv"):
        self.filename = filename
        self.remaining = total
        self.served = 0

    async def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        n = self.remaining if size is None or size < 0 else min(size, self.remaining)
        self.remaining -= n
        self.served += n
        return b"x" * n


def test_a_file_under_the_limit_is_read_whole():
    up = FakeUpload(3 * 1024 * 1024)
    data = asyncio.run(main.read_upload_capped(up, limit=8 * 1024 * 1024))
    assert len(data) == 3 * 1024 * 1024


def test_a_file_over_the_limit_is_refused_with_413():
    up = FakeUpload(9 * 1024 * 1024)
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.read_upload_capped(up, limit=4 * 1024 * 1024))
    # 413, not 400: the request is well formed, the size is what is
    # unacceptable, and a client should be able to tell those apart.
    assert e.value.status_code == 413


def test_the_refusal_names_the_file_and_the_limit():
    up = FakeUpload(9 * 1024 * 1024, filename="year_of_gateway.csv")
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.read_upload_capped(up, limit=4 * 1024 * 1024))
    detail = str(e.value.detail)
    assert "year_of_gateway.csv" in detail
    assert "4 MB" in detail
    # Say how to proceed, not just that it failed.
    assert "MAX_UPLOAD_MB" in detail


def test_it_stops_reading_instead_of_buffering_the_whole_file():
    limit = 4 * 1024 * 1024
    up = FakeUpload(512 * 1024 * 1024)          # half a gigabyte
    with pytest.raises(HTTPException):
        asyncio.run(main.read_upload_capped(up, limit=limit))
    # The whole point. Allow one chunk of overshoot — the limit is detected on
    # the chunk that crosses it — but nothing like the full file.
    assert up.served <= limit + main._UPLOAD_CHUNK, (
        f"consumed {up.served} bytes to enforce a {limit}-byte limit; the read "
        f"is buffering the file it is supposed to be refusing"
    )


def test_an_empty_upload_is_not_an_error():
    # Empty files are handled downstream ("if not content: return"), and the
    # cap must not turn that into a failure.
    assert asyncio.run(main.read_upload_capped(FakeUpload(0))) == b""


def test_every_upload_path_goes_through_the_cap():
    """
    Reading the source, because the risk is a NEW endpoint added later with a
    bare read rather than the four that exist today being un-fixed.
    """
    source = (SRC / "main.py").read_text(encoding="utf-8")
    bare = [
        line.strip()
        for line in source.splitlines()
        if "await" in line and ".read()" in line and not line.strip().startswith("#")
    ]
    assert not bare, (
        "these uploads bypass read_upload_capped and are unbounded: " + str(bare)
    )


def test_the_limit_is_configurable_and_sane():
    assert main.MAX_UPLOAD_BYTES >= 1024 * 1024
    # Comfortably above the 50K stress corpus (~5 MB) so real exports pass.
    assert main.MAX_UPLOAD_BYTES >= 32 * 1024 * 1024
