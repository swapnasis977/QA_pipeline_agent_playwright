"""A heal may change HOW a test finds or drives something — never WHAT it proves.

The Self-Healer is told this in its skill, and a skill is only a request. The
failure it exists to prevent: a real product bug is misclassified as a fixable
test issue, the healer "fixes" it by weakening the assertion, the re-run goes
green, and the case is reported healed — a defect hidden behind a passing test.

This is the check that holds in code: compare the file before and after the
patch and refuse one that proves less than it did.

Pure text analysis (no parser). Four kinds of finding:

  1. FEWER assertions, or fewer tests.
  2. A CHANGED assertion: the multiset of (matcher, argument) pairs must not lose
     anything. Swapping `toHaveText('Saved')` for `toBeVisible()` leaves the
     `expect(` count unchanged, so counting calls alone let it through.
  3. Constructs whose only purpose is to make a step stop failing: swallowed
     errors (`.catch(...)` in any form), `try {}` blocks, `test.skip/fixme`, a
     bare `toBeTruthy()`, and a hard `waitForTimeout`.
  4. `force: true`, which is sometimes the CORRECT fix (a real <select> hidden
     behind a styled widget, a sticky header covering its own button) and is
     sometimes a way to click something a user cannot. It is allowed only when
     the line above carries `// qa-allow-force: <reason>`, so the exception is
     visible in the diff and a reviewer can judge the reason.
"""

from __future__ import annotations

import re
from collections import Counter

_EXPECT_RE = re.compile(r"\bexpect(?:\.soft|\.poll)?\s*\(")
_TEST_RE = re.compile(r"(?m)^\s*test(?:\.\w+)?\s*\(\s*['\"`]")

#: `...).toHaveText('x')`, `...).not.toBeVisible()`. One level of nested
#: parentheses inside the argument list (e.g. `toHaveText(/a(b)/)`).
_MATCHER_RE = re.compile(
    r"\)\s*\.\s*(not\s*\.\s*)?(to[A-Z]\w*)\s*\(((?:[^()]|\([^()]*\))*)\)")

#: (label, pattern) — constructs that make a step stop failing instead of
#: making it right. Only an INCREASE in their count is a finding.
_WEAKENERS = (
    ("a swallowed error (.catch(...))", re.compile(r"\.catch\s*\(")),
    ("a try/catch block", re.compile(r"\btry\s*\{")),
    ("test.skip / test.fixme", re.compile(r"\btest\.(?:skip|fixme)\b")),
    ("a bare toBeTruthy()/toBeDefined()", re.compile(r"\.(?:toBeTruthy|toBeDefined)\s*\(\s*\)")),
    ("a hard waitForTimeout()", re.compile(r"\bwaitForTimeout\s*\(")),
)

_FORCE_RE = re.compile(r"\bforce\s*:\s*true\b")
_FORCE_OK_RE = re.compile(r"qa-allow-force:\s*\S")

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"(?m)^\s*//.*$")


def _code(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text or ""))


def _norm_arg(arg: str) -> str:
    """Whitespace- and quote-insensitive, so a reformat is not a change."""
    return re.sub(r"\s+", " ", arg.replace('"', "'").replace("`", "'")).strip()


def _matchers(code: str) -> Counter:
    return Counter((bool(m.group(1)), m.group(2), _norm_arg(m.group(3)))
                   for m in _MATCHER_RE.finditer(code))


def _unannotated_force(text: str) -> int:
    """`force: true` uses with no `// qa-allow-force: <reason>` on the same or the
    previous non-blank line. Comments are NOT stripped here: the annotation IS a
    comment."""
    lines = (text or "").splitlines()
    count = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("//") or not _FORCE_RE.search(line):
            continue
        previous = next((lines[j] for j in range(i - 1, -1, -1) if lines[j].strip()), "")
        if not (_FORCE_OK_RE.search(line) or _FORCE_OK_RE.search(previous)):
            count += 1
    return count


def weakened(before: str, after: str) -> list:
    """Reasons the patch proves less than the original, [] when it does not."""
    old, new = _code(before), _code(after)
    reasons = []

    removed = len(_EXPECT_RE.findall(old)) - len(_EXPECT_RE.findall(new))
    if removed > 0:
        reasons.append(f"it removes {removed} assertion(s) (expect calls: "
                       f"{len(_EXPECT_RE.findall(old))} -> {len(_EXPECT_RE.findall(new))})")

    lost = _matchers(old) - _matchers(new)
    if lost:
        shown = ", ".join(
            f"{'not.' if neg else ''}{name}({arg})" for (neg, name, arg) in list(lost)[:3])
        reasons.append(f"it changes what is asserted - these assertions no longer "
                       f"appear: {shown}")

    dropped = len(_TEST_RE.findall(old)) - len(_TEST_RE.findall(new))
    if dropped > 0:
        reasons.append(f"it drops {dropped} test(s)")

    for label, pattern in _WEAKENERS:
        added = len(pattern.findall(new)) - len(pattern.findall(old))
        if added > 0:
            reasons.append(f"it adds {added} use(s) of {label}")

    forced = _unannotated_force(after) - _unannotated_force(before)
    if forced > 0:
        reasons.append(f"it adds {forced} use(s) of force: true without a "
                       f"`// qa-allow-force: <reason>` comment on the line above")
    return reasons
