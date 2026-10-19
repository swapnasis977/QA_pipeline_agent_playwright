"""A per-run log file, without rewriting every `print`.

The pipeline reports progress with ~290 `print` calls and no logging framework, so a
run that scrolled off the terminal (or ran in CI) left nothing to read afterwards.
Migrating every call to `logging` is a large change with its own risks; teeing the two
console streams into a file captures all of them at once, including output from code
that has not been touched yet.

The file gets a timestamp at the start of every line; the console is unchanged.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


class _Tee:
    def __init__(self, stream: TextIO, log: TextIO, lock: threading.Lock) -> None:
        self._stream, self._log, self._lock = stream, log, lock
        self._at_line_start = True

    def write(self, text: str) -> int:
        written = self._stream.write(text)
        try:
            with self._lock:
                for chunk in text.splitlines(keepends=True):
                    if self._at_line_start:
                        self._log.write(datetime.now().strftime("%H:%M:%S  "))
                    self._log.write(chunk)
                    self._at_line_start = chunk.endswith("\n")
                self._log.flush()
        except (OSError, ValueError):
            pass   # a full disk or a closed log must never break the run it is recording
        return written

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:          # encoding, isatty, fileno ...
        return getattr(self._stream, name)


@contextlib.contextmanager
def tee_to(path: str | Path) -> Iterator[Path | None]:
    """While active, everything printed to stdout/stderr is also appended to `path`."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        log = target.open("a", encoding="utf-8", errors="replace")
    except OSError:
        yield None            # cannot open a log: run without one
        return
    lock = threading.Lock()
    log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')}  "
              f"{' '.join(sys.argv[1:])[:200]} =====\n")
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(original_out, log, lock), _Tee(original_err, log, lock)
    try:
        yield target
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        log.close()
