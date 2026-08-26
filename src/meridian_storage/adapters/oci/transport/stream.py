# SPDX-License-Identifier: Apache-2.0
"""Replayable bounded response streams registered outside Core JSON envelopes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from io import RawIOBase
from typing import BinaryIO

from meridian_storage.object_common import PayloadSource


class IteratorReader(RawIOBase):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        super().__init__()
        self._chunks = chunks
        self._buffer = bytearray()
        self._ended = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed registry stream")
        if size == 0:
            return b""
        if size < 0:
            for chunk in self._chunks:
                self._buffer.extend(chunk)
            self._ended = True
            result = bytes(self._buffer)
            self._buffer.clear()
            return result
        while len(self._buffer) < size and not self._ended:
            try:
                self._buffer.extend(next(self._chunks))
            except StopIteration:
                self._ended = True
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result


class RegistryBlobSource(PayloadSource):
    def __init__(self, opener: Callable[[], AbstractContextManager[BinaryIO]]) -> None:
        self._opener = opener

    @property
    def replayable(self) -> bool:
        return True

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        with self._opener() as stream:
            yield stream


__all__ = ["IteratorReader", "RegistryBlobSource"]
