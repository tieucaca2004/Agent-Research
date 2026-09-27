"""Bounded body decoding.

httpx's own decoders can inflate one network chunk into an unbounded buffer (probe E9: 49 KiB
→ 50 MiB in one chunk). The crawler therefore reads *raw* bytes and inflates them here with
``zlib``'s ``max_length``, so output per step is bounded and the limit on decoded bytes is
enforced before memory grows (probe E10).
"""

from __future__ import annotations

import zlib

SUPPORTED_ENCODINGS = frozenset({"identity", "gzip", "x-gzip", "deflate"})
_STEP = 64 * 1024


class BodyTooLarge(Exception):
    pass


class BodyDecodeError(Exception):
    pass


class UnsupportedEncoding(Exception):
    pass


class BoundedDecoder:
    def __init__(self, content_encoding: str | None, *, max_bytes: int) -> None:
        encoding = (content_encoding or "identity").strip().lower() or "identity"
        if "," in encoding or encoding not in SUPPORTED_ENCODINGS:
            raise UnsupportedEncoding(encoding)
        self.encoding = encoding
        self._max = max_bytes
        self._chunks: list[bytes] = []
        self.size = 0
        self._zlib: zlib._Decompress | None = None
        self._deflate_pending = b""
        if encoding in ("gzip", "x-gzip"):
            self._zlib = zlib.decompressobj(16 + zlib.MAX_WBITS)

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        self.size += len(data)
        if self.size > self._max:
            raise BodyTooLarge
        self._chunks.append(data)

    def _inflate(self, data: bytes) -> None:
        if self._zlib is None:
            raise BodyDecodeError
        buf = data
        while buf:
            try:
                out = self._zlib.decompress(buf, _STEP)
            except zlib.error:
                raise BodyDecodeError from None
            self._emit(out)
            buf = self._zlib.unconsumed_tail

    def feed(self, data: bytes) -> None:
        if self.encoding == "identity":
            self._emit(data)
            return
        if self.encoding == "deflate" and self._zlib is None:
            # "deflate" is zlib-wrapped per RFC, but some servers send raw deflate.
            self._deflate_pending += data
            if len(self._deflate_pending) < 2:
                return
            first, second = self._deflate_pending[0], self._deflate_pending[1]
            zlib_wrapped = (first & 0x0F) == 8 and ((first << 8) | second) % 31 == 0
            self._zlib = zlib.decompressobj(zlib.MAX_WBITS if zlib_wrapped else -zlib.MAX_WBITS)
            data, self._deflate_pending = self._deflate_pending, b""
        self._inflate(data)

    def finish(self) -> bytes:
        if self.encoding == "deflate" and self._zlib is None and self._deflate_pending:
            self._zlib = zlib.decompressobj(-zlib.MAX_WBITS)
            pending, self._deflate_pending = self._deflate_pending, b""
            self._inflate(pending)
        if self._zlib is not None:
            try:
                # Drain buffered output in bounded steps; flush()'s argument is only an
                # initial buffer size, not a limit, so it runs last on an empty buffer.
                while True:
                    out = self._zlib.decompress(self._zlib.unconsumed_tail, _STEP)
                    if not out:
                        break
                    self._emit(out)
                self._emit(self._zlib.flush())
            except zlib.error:
                raise BodyDecodeError from None
            if not self._zlib.eof and self.encoding != "deflate":
                raise BodyDecodeError  # truncated gzip stream
        return b"".join(self._chunks)
