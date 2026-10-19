"""
Loads the "app design doc" — a human-written reference for the TARGET
APPLICATION itself, shared by the Test Case Writer and the Coding Agent.

Why this exists (a real gap this session's runs exposed): before this file,
nothing gave either agent durable, app-specific knowledge. Each one only saw
fragments assembled fresh per run — JIRA text, a DOM snapshot of one screen,
POM_SUMMARY.md's confirmed locators — so the SAME facts a human already knows
about the app (its real screen names, its navigation structure, that a device
is mocked in this environment, that "tile" in a test case means "module
button" in the UI) were re-guessed or missed every run instead of stated once.

This is deliberately NOT the same thing as:
  - instructions/conventions.md — pipeline-wide HOUSE STYLE (naming, output
    format, never-fabricate), true for every target app this pipeline is
    ever pointed at. This file is the opposite: ONE app's actual facts.
  - POM_SUMMARY.md / locator_memory.json — machine-derived, updated
    automatically from confirmed runs. This file is hand-written and stable;
    it doesn't change just because a run happened.
  - --conventions-file (agents/coding_agent.py) — CODING conventions (file
    structure, fixture usage), not domain/product knowledge, and not read by
    the Test Case Writer at all.

Format: plain markdown, no required structure — write whatever a new team
member would need to read before writing a test case or a script for this
app: real screen/button names (and the domain wording testers use for them,
so the gap is bridged explicitly), the navigation map, environment quirks
(a mocked device, a known-flaky third-party redirect), anything a human
tester already knows that an agent currently has to guess.

Optional throughout: an app with no design doc behaves exactly as before
this module existed.
"""

from pathlib import Path

from agents.artifact_io import read_text_file


def load(path) -> str:
    """Reads the app design doc, or "" if none was given/it doesn't exist.
    Never raises — a missing or unreadable doc must degrade to "no extra
    context", not break either agent's run."""
    if not path:
        return ""
    return read_text_file(Path(path))


def as_extra_system(doc_text: str) -> str:
    """Wraps the doc text with a header identifying what it is and how much
    to trust it, so a model reading it alongside JIRA text or a DOM snapshot
    knows this is STANDING, human-authored knowledge about the real app —
    not per-case generated content, and not itself a source to copy from
    verbatim if it conflicts with something more specific (a live DOM
    snapshot always wins over this doc's prose, since the app can change
    after the doc was written)."""
    if not doc_text.strip():
        return ""
    return (
        "Application design / domain reference (human-authored, describes the REAL "
        "target application — screen names, navigation, environment quirks). Use it "
        "to write test cases and automation code that match the app's ACTUAL UI "
        "vocabulary and structure, not invented or guessed terminology. If this doc "
        "and a live DOM snapshot given elsewhere in this prompt disagree about the "
        "CURRENT state of a screen, the live snapshot wins — the app may have changed "
        "since this doc was written; this doc is background knowledge, not a "
        "substitute for what's actually on screen right now:\n\n" + doc_text.strip()
    )
