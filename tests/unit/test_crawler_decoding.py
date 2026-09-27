"""Sprint 04: bounded decompression, content-type and charset helpers."""

import tracemalloc

import pytest

from research_agent.crawler.crawler import _choose_charset, _media_type
from research_agent.crawler.decoding import (
    BodyDecodeError,
    BodyTooLarge,
    BoundedDecoder,
    UnsupportedEncoding,
)
from tests.crawler_support import gzip_bytes, raw_deflate, zlib_deflate

MIB = 1024 * 1024


def decode(encoding: str | None, data: bytes, limit: int, chunk: int = 16_384) -> bytes:
    d = BoundedDecoder(encoding, max_bytes=limit)
    for i in range(0, len(data), chunk):
        d.feed(data[i : i + chunk])
    return d.finish()


def test_identity_exact_limit_and_one_over() -> None:
    assert len(decode(None, b"a" * 1000, 1000)) == 1000
    with pytest.raises(BodyTooLarge):
        decode("identity", b"a" * 1001, 1000)


@pytest.mark.parametrize("compress", [gzip_bytes, zlib_deflate, raw_deflate])
def test_gzip_and_deflate_roundtrip(compress: object) -> None:
    data = b"<html>" + b"x" * 50_000 + b"</html>"
    encoding = "gzip" if compress is gzip_bytes else "deflate"
    assert decode(encoding, compress(data), 100_000) == data  # type: ignore[operator]


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [("gzip", gzip_bytes), ("deflate", zlib_deflate), ("deflate", raw_deflate)],
)
def test_decompression_bomb_is_stopped_with_bounded_memory(encoding: str, compress: object) -> None:
    bomb = compress(b"\0" * (50 * MIB))  # type: ignore[operator]
    assert len(bomb) < 200_000
    tracemalloc.start()
    try:
        with pytest.raises(BodyTooLarge):
            decode(encoding, bomb, 1 * MIB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * MIB, peak  # httpx's own decoder peaked at 127 MiB here (probe E9)


def test_single_huge_chunk_is_still_bounded() -> None:
    bomb = gzip_bytes(b"\0" * (20 * MIB))
    tracemalloc.start()
    try:
        with pytest.raises(BodyTooLarge):
            decode("gzip", bomb, MIB, chunk=len(bomb))  # whole payload in one feed()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * MIB


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip", "gzip,deflate"])
def test_unsupported_encodings(encoding: str) -> None:
    with pytest.raises(UnsupportedEncoding):
        BoundedDecoder(encoding, max_bytes=100)


def test_corrupt_and_truncated_gzip() -> None:
    with pytest.raises(BodyDecodeError):
        decode("gzip", b"not gzip at all", 1000)
    full = gzip_bytes(b"hello world" * 100)
    with pytest.raises(BodyDecodeError):
        decode("gzip", full[: len(full) // 2], 10_000)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("text/html; charset=UTF-8", ("text/html", "utf-8")),
        ("TEXT/PLAIN", ("text/plain", None)),
        ('text/html; charset="windows-1258"', ("text/html", "windows-1258")),
        ("application/xhtml+xml;charset=iso-8859-1", ("application/xhtml+xml", "iso-8859-1")),
        (None, (None, None)),
        ("", (None, None)),
        ("garbage", (None, None)),
        ("text/", (None, None)),
        ("/html", (None, None)),
    ],
)
def test_media_type_parsing(header: str | None, expected: tuple[str | None, str | None]) -> None:
    assert _media_type(header) == expected


def test_charset_selection() -> None:
    assert _choose_charset("utf-8", b"x") == "utf-8"
    assert _choose_charset(None, b"\xef\xbb\xbfhello") == "utf-8-sig"
    assert _choose_charset(None, b'<meta charset="windows-1258">') == "cp1258"
    assert _choose_charset("no-such-charset", b"x") == "utf-8"
    assert _choose_charset(None, b"plain") == "utf-8"
