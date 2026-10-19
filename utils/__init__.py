"""Package init for utils/.

Reconfigures stdout/stderr to UTF-8, mirroring agents/__init__.py.

This is not redundant with that one. Several modules in this package print
non-ASCII status markers ("⚠️", "✅", em dashes) — utils/dom_inspector.py
does so from inside its browser-launch FAILURE path. On Windows the default
console encoding is cp1252, which cannot encode those characters, so the
print raises UnicodeEncodeError. When that happens inside an error handler,
the crash REPLACES the diagnostic it was trying to emit: instead of
"could not launch Chromium (no display)" the caller gets a
UnicodeEncodeError traceback from the encodings module, pointing nowhere
near the real problem. Observed exactly that way.

agents/__init__.py only helps when something under agents/ is imported
first. That holds for `python -m qa_pipeline.cli` and `python -m agents.<name>`, but
not for a utils-first import path (`from utils import dom_inspector`, a
test module, or any future utils-level entry point) — and the import order
of a package is not something a print statement three modules deep should
depend on for correctness. Doing it in both package inits makes it
order-independent; the call is idempotent, so whichever runs first wins and
the second is a no-op.
"""

import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")
