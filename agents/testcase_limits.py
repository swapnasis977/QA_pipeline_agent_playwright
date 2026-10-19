"""Concurrency limits shared by the test-case writer and its importer.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""

from __future__ import annotations

#: Stories are independent LLM calls (no cross-story reasoning), so a
#: multi-story feature runs them concurrently — capped conservatively to
#: stay well under most providers' per-minute rate limits on one API key.
_MAX_PARALLEL_STORIES = 4
