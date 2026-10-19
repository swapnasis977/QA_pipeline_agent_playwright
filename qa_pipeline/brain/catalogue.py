"""The element vocabulary the Planner is allowed to use.

WHY THIS EXISTS. The Planner used to receive only the test case and a POM
summary — it never saw the ledger. So it invented element names out of the
test case's prose ("Post code field", "Check OIMS checkbox") and invented
selectors to match (`#Vendortype`, which does not exist; the real element
is `#Vendortype_select`). `gate_plan` then resolved those invented names
against the ledger, failed most of them, and the repair loop spent its budget
asking a model to guess again with no more information than the first time.

Observed on a live Vendor run: 11 of 38 elements unresolved on the first
pass, 6 after two repairs, and the Coding Agent produced zero files across
four attempts spanning two sessions.

The fix is not a better prompt. It is giving the Planner the actual
vocabulary: every element the ledger can ground, grouped by screen, with the
real logical name and selector. Naming then stops being generation and
becomes SELECTION from a closed list, which is the difference between a model
guessing and a model choosing.

WHY THE SELECTOR IS SHOWN TOO. Some elements have no accessible name at all —
this application has four on the Vendor Profile Details screen alone
(`#CheckOIMS_1`, `#Company`, `#addressInfoLine1`, `#addressInfoZipCode`: no
placeholder, no aria-label, no label). No amount of re-recording gives those
a human-readable name, so a Planner working from prose can never address
them. Showing the selector lets it say "the element whose id is
addressInfoZipCode" and be exactly right.
"""

from __future__ import annotations

#: Cap the rendered catalogue so a large ledger cannot crowd out the test case
#: itself in the prompt. Screens named by the plan are rendered first, so the
#: truncation falls on screens the case does not mention.
DEFAULT_CHAR_BUDGET = 12000


def _value(card: dict) -> dict:
    return card.get("value") or {}


def collect(ledger, screens: list = None) -> dict:
    """Groundable elements grouped by screen: {screen: [(name, locator, role), ...]}.

    Only cards that may legally supply a locator are included — an element
    known solely from a design doc is not vocabulary the Planner may use,
    because the Coding Agent will refuse it downstream anyway. Offering it
    here would just move the failure later.
    """
    from qa_pipeline.brain import trust

    ledger.load()
    wanted = {(s or "").strip().lower() for s in (screens or []) if s}
    by_screen: dict = {}

    for card in ledger.by_type("element"):
        if not trust.can_ground_locator(card):
            continue
        value = _value(card)
        name = (value.get("logical_name") or "").strip()
        if not name:
            continue
        screen = (value.get("screen") or "").strip() or "(unknown screen)"
        if wanted and screen.lower() not in wanted:
            continue
        # The ROLE decides how the element may be interacted with, and it is
        # not inferable from the name. `#Vendortype_select` on this
        # application is an <a>, not a <select>: a Coding Agent that read the
        # name alone called `.selectOption()` on it and failed at run time
        # with "Element is not a <select> element". Carrying the role — and
        # saying plainly when it is unknown — is what prevents that.
        entry = (name, (value.get("locator") or "").strip(),
                 (value.get("role") or "").strip())
        bucket = by_screen.setdefault(screen, [])
        if entry not in bucket:
            bucket.append(entry)

    for bucket in by_screen.values():
        bucket.sort(key=lambda entry: entry[0].lower())
    return by_screen


def render(ledger, screens: list = None, char_budget: int = DEFAULT_CHAR_BUDGET,
           only_screens: bool = False) -> str:
    """The catalogue as prompt text, or "" when the ledger knows nothing.

    Returning "" rather than a placeholder matters: an empty catalogue must
    leave the Planner's behaviour exactly as it was, so a project whose ledger
    has not been populated yet is not handed an instruction it cannot follow.
    """
    # `screens` PRIORITISES by default (the Planner wants the rest as fallback
    # vocabulary). With `only_screens=True` the heal stage gets just those
    # screens, so its block stays small; a scope matching nothing falls back to
    # the whole ledger rather than rendering nothing.
    by_screen = collect(ledger, screens) if (screens and only_screens) else collect(ledger)
    if screens and only_screens and not by_screen:
        by_screen = collect(ledger)
    if not by_screen:
        return ""

    # Screens the case actually mentions come first, so truncation lands on
    # the ones it does not. Compared case-insensitively: plan screens read
    # "Dashboard" while ledger screens may be stored casefolded.
    wanted = {" ".join(str(s).split()).casefold() for s in (screens or []) if s}
    named = sorted(s for s in by_screen if " ".join(s.split()).casefold() in wanted)
    rest = sorted(s for s in by_screen if s not in named)

    lines = []
    used = 0
    truncated = False
    for screen in named + rest:
        block = [f"## {screen}"]
        for name, locator, role in by_screen[screen]:
            bits = f"  - {name}"
            if locator:
                bits += f"   [{locator}]"
            bits += f"   role={role}" if role else "   role=UNKNOWN"
            block.append(bits)
        text = "\n".join(block)
        if used + len(text) > char_budget:
            truncated = True
            break
        lines.append(text)
        used += len(text)

    if not lines:
        return ""

    header = (
        "ELEMENT VOCABULARY — these are the ONLY elements that exist.\n"
        "Every element you name in a plan MUST be copied verbatim from this\n"
        "list, including its exact spelling and capitalisation. The bracketed\n"
        "value is that element's real selector on the running application.\n"
        "\n"
        "Do NOT invent an element name from the test case's wording, and do\n"
        "NOT invent a selector. If the step you need cannot be expressed with\n"
        "anything in this list, say so in that step's `unresolved` field\n"
        "rather than naming something that is not here — a named element that\n"
        "does not exist fails the plan gate and costs a repair cycle, while an\n"
        "honest `unresolved` is handled properly.\n"
        "\n"
        "Some elements have no human-readable name because the application\n"
        "gives them none (no label, no placeholder, no aria-label). Those\n"
        "appear under their identifier — use it as written.\n"
        "\n"
        "NEVER INFER HOW TO INTERACT WITH AN ELEMENT FROM ITS NAME — use the\n"
        "role. On this application `#Vendortype_select` is an <a>, not a\n"
        "<select>: calling selectOption() on it fails at run time with\n"
        "\"Element is not a <select> element\". Only use selectOption() where\n"
        "the role is combobox or listbox. Where the role is UNKNOWN, click()\n"
        "and fill() are the safe choices — a name ending in `_select`,\n"
        "`_input` or `_button` says nothing about the real tag.\n"
    )
    body = "\n\n".join(lines)
    if truncated:
        body += "\n\n(further screens omitted to fit the prompt budget)"
    return f"{header}\n{body}"
