"""Module to read script(1) advanced timing (and referenced output/input) files."""

from __future__ import annotations

import io
import os.path
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer


def get_base_path(actual_path: Path, rel_path: Path) -> Optional[Path]:
    """Returns base_path such that actual_path == base_path / rel_path."""
    if rel_path.is_absolute():
        return None
    actual_path = actual_path.resolve()
    actual_parts = actual_path.parts
    rel_parts = rel_path.parts
    if os.path.commonprefix([actual_parts[::-1], rel_parts[::-1]]) != rel_parts[::-1]:
        # paths don't overlap
        return None
    return Path(*actual_parts[: -len(rel_parts)])


class Direction(Enum):
    OUT = "O"
    IN = "I"


@dataclass
class Entry:
    time: float
    direction: Direction
    size: int


class ScriptLog(io.RawIOBase):
    def __init__(self, timing_path: Path):
        super().__init__()
        self.entries: List[Entry] = []
        self.info: Dict[str, str] = {}
        self.output_path: Optional[Path] = None
        self.input_path: Optional[Path] = None

        # IO stream fields
        self._closed = False
        self._output_stream: Optional[IO[bytes]] = None
        # length of remaining output (excludes header and footer lines)
        self._remaining_bytes = 0

        self._read_headers(timing_path)

    def _read_headers(self, timing_path: Path) -> None:
        with open(timing_path) as timing_file:
            base_path = None
            time = 0.0
            for line in timing_file:
                entry_type, elapsed, *rest = line.rstrip("\n").split(" ")
                try:
                    time += float(elapsed)
                except ValueError:
                    raise ValueError(
                        "Invalid script(1) timing file (is it in the advanced format?)"
                    ) from None
                if entry_type == "H":
                    key = rest[0]
                    value = " ".join(rest[1:])
                    self.info[key] = value
                    if key == "TIMING_LOG":
                        # determine the path where script was executed, based on the
                        # paths to the timing log file
                        base_path = get_base_path(timing_path.resolve(), Path(value))
                    if key == "OUTPUT_LOG":
                        self.output_path = Path(value)
                        if base_path is not None:
                            self.output_path = base_path / self.output_path
                    if key == "INPUT_LOG":
                        self.input_path = Path(value)
                        if base_path is not None:
                            self.input_path = base_path / self.input_path
                elif entry_type == "O":
                    size = int(rest[0])
                    self.entries.append(
                        Entry(time=time, direction=Direction.OUT, size=size)
                    )
                    self._remaining_bytes += size
                elif entry_type == "I":
                    size = int(rest[0])
                    self.entries.append(
                        Entry(time=time, direction=Direction.IN, size=size)
                    )
                elif entry_type == "S":
                    # signal
                    pass
                else:
                    raise ValueError(
                        f"Unknown script(1) timing entry type {entry_type!r}"
                    )

    def readable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._output_stream is not None:
            self._output_stream.close()

    def readinto(self, buffer: WriteableBuffer) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file.")
        if self._output_stream is None:
            # only executed on first call
            if self.output_path is None:
                raise ValueError("Output not captured by script(1)")
            # pylint: disable-next=consider-using-with
            self._output_stream = open(self.output_path, mode="rb")
            # skip header
            self._output_stream.readline()
        n_bytes = min(len(buffer), self._remaining_bytes)  # type: ignore[arg-type]
        read_bytes = self._output_stream.read(n_bytes)
        buffer[: len(read_bytes)] = read_bytes  # type: ignore[call-overload, index]
        self._remaining_bytes -= len(read_bytes)
        return n_bytes
