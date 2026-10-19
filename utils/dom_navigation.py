"""Navigation and login heuristics for utils/dom_inspector.py, kept in their own module so
that one stays under its size ratchet: keyword extraction (stopwords, destructive-control
deny list) and the identity-provider URL check. Pure functions; no browser needed."""

from __future__ import annotations

import re
from urllib.parse import urlparse

#: Max keywords pulled from a flow description per inspection.
MAX_KEYWORDS = 12

#: Filler words that are 4+ letters but never name a screen or control. The
#: first-12-words cap used to be spent on them ("verify", "that", "with",
#: "user", "when"), pushing the real navigation words out of the budget.
KEYWORD_STOPWORDS = frozenset([
    "verify", "that", "with", "user", "when", "then", "should", "shall", "which", "their",
    "there", "this", "from", "have", "been", "after", "before", "into", "while", "also",
    "only", "each", "such", "than", "over", "will", "would", "being", "does", "correctly",
    "properly", "displays", "display", "shows", "show", "ensure", "check", "test", "case",
    "steps", "step", "expected", "result", "results", "given", "able", "ability", "valid",
    "invalid", "successfully", "navigate", "open", "click", "select", "perform", "performs",
])

#: Controls the keyword/fuzzy navigation must never click: inspection is meant
#: to LOOK, and a keyword like "Remove" or "Sign out" matched a real control
#: and destroyed the session or the data being inspected.
DESTRUCTIVE_RE = re.compile(r"log\s?out|sign\s?out|delete|remove|discard|cancel", re.I)


def is_destructive(text: str) -> bool:
    return bool(DESTRUCTIVE_RE.search(text or ""))


def extract_keywords(text: str, limit: int = MAX_KEYWORDS) -> list:
    """Up to `limit` distinct 4+ letter navigation words from `text`, without
    filler words or destructive verbs, in order of appearance."""
    out, seen = [], set()
    for word in re.findall(r"[A-Za-z]{4,}", text or ""):
        low = word.lower()
        if low in seen or low in KEYWORD_STOPWORDS or is_destructive(word):
            continue
        seen.add(low)
        out.append(word)
        if len(out) >= limit:
            break
    return out


#: Hosts that mean we are still at the identity provider rather than in the
#: app. Leaving them is a far stronger success signal than "no password field
#: is visible": the KMSI screen has no password field either.
IDP_HOST_MARKERS = ("login.microsoftonline.com", "login.microsoft.com",
                    "sts.", "adfs", "okta.com", "auth0.com", "login.live.com")


def is_idp_url(url: str) -> bool:
    """Whether the URL's HOST (not its path or query) is an identity provider.

    Markers used to be matched as bare substrings of the whole URL, so
    `tests.corp`, `posts.example` or `?next=/sts.` counted as an IDP. A marker
    now matches the hostname by equality, as a dotted suffix, or (for the
    trailing-dot prefix markers like `sts.`) as the host's FIRST label.
    """
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False
    labels = host.split(".")
    for marker in IDP_HOST_MARKERS:
        m = marker.lower()
        if m.endswith("."):  # prefix marker: the first label itself
            if labels[0] == m[:-1]:
                return True
        elif "." in m:
            if host == m or host.endswith("." + m):
                return True
        elif any(label == m or label.startswith(m + "-") for label in labels):
            return True
    return False
