"""The brain — a ledger of sourced facts about the target application.

Not a document store. Documents are an INPUT to this (see `ingest`); so is a
live crawl (`explore`) and so is a passing test run (`writeback`). What the
ledger holds is the distilled, typed, deduplicated result: one card per fact,
each carrying where it came from and how much it can be trusted.

Four entry points, in the order a run touches them:

    ingest.from_documents(...)   design docs / Figma / legacy sheets -> cards
    explore.crawl(...)           the live application               -> cards
    retrieve.for_agent(...)      ledger + a task                    -> prompt block
    writeback.from_run(...)      a green run                        -> promotions

Typical use:

    from qa_pipeline import brain
    ledger = brain.open_ledger(profile.brain_dir)
    knowledge = brain.retrieve.for_agent(ledger, "coding_agent", case_text)
    ...
    brain.writeback.from_run(ledger, run_result, files)
    ledger.save()
"""

from . import cards, explore, ingest, retrieve, trust, writeback  # noqa: F401
from .store import Ledger, open_ledger  # noqa: F401

__all__ = ["Ledger", "open_ledger", "cards", "ingest", "explore",
           "retrieve", "trust", "writeback"]
