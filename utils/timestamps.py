"""UTC timestamps for the pipeline's on-disk artifacts.

`datetime.utcnow()` is deprecated since Python 3.12 and SCHEDULED FOR
REMOVAL, so it is not merely noisy — it will stop working. It is also
genuinely wrong: it returns a NAIVE datetime holding UTC wall-clock time,
so the value claims no timezone at all. Every call site then appended a
literal "Z" to assert UTC anyway, which is only correct by convention and
silently produces a wrong label the moment someone reformats it or feeds it
to something that localizes naive datetimes.

Both replacements here are timezone-AWARE. They are kept in one module, with
the reasoning, so `utcnow()` doesn't get reintroduced by the next person who
needs a timestamp and reaches for the obvious name.

The output STRING FORMATS are byte-identical to what the previous
`utcnow()`-based calls produced, because these values are already persisted
in existing artifacts (token_log.json timestamps, pipeline_errors.json
timestamps, summary.<stamp>.md filenames). Changing the shape would make a
run's new entries inconsistent with the ones already in the same file.
"""

from datetime import datetime, timezone


def utc_iso() -> str:
    """ISO-8601 UTC with a trailing "Z", e.g. "2026-09-02T14:03:59.123456Z".

    Replaces `datetime.utcnow().isoformat() + "Z"`. An aware datetime's
    isoformat() ends in "+00:00" rather than "Z", so that suffix is
    normalized back to "Z" to keep the format identical to every timestamp
    already written into token_log.json and pipeline_errors.json.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_stamp(fmt: str) -> str:
    """strftime against the current UTC time.

    Replaces `datetime.utcnow().strftime(fmt)`. strftime ignores tzinfo for
    every format code used here, so an aware datetime produces the same
    string — e.g. utc_stamp("%Y%m%d_%H%M%S") for an archived
    summary.<stamp>.md filename.
    """
    return datetime.now(timezone.utc).strftime(fmt)
