"""Agents — new ones here, existing ones reached through `adapters`.

    framework_scaffolder   NEW, deterministic. Writes the fixed POM skeleton.
    adapters               Contract-validated wrappers over the existing
                           agents/ package (writer, planner, coder, executor,
                           reviewer, healer, reporter, jira).

The two other new agents do not live here because they are not really
stage-shaped — they are the brain filling itself in, so they live with it:

    brain.ingest    documents  -> knowledge cards  (the Document Ingestor)
    brain.explore   the live app -> knowledge cards (the App Explorer)

Keeping them next to the ledger they write to, rather than in an `agents/`
folder for symmetry's sake, means the trust rules and the cards they produce
are one import away from the code producing them.
"""

from . import adapters, framework_scaffolder  # noqa: F401

__all__ = ["adapters", "framework_scaffolder"]
