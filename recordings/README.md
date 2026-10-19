# Recordings

Human touchpoint 2, and the only one that is not approval: click through a
screen once so the pipeline never has to guess its locators again.

This is where the escalation ladder ends. When the App Explorer cannot crawl
to a screen — a hardware-gated state, an external redirect, a seeded record —
the alternative to a person clicking through once is a model guessing, which
is the thing the whole architecture exists to prevent. A recording becomes the
highest-confidence source in the ledger, permanently, so the same screen is
never asked for twice.

Drop `npx playwright codegen $APP_BASE_URL` output here, one file per test case:

```bash
npx playwright codegen $APP_BASE_URL
# perform the flow, save the generated script as recordings/<TestCaseID>.ts
```

If codegen's own suggested filename doesn't match a real Test Case ID, don't
rename the file — add an entry to `manifest.json` instead:

```json
{ "codegens-suggested-filename": "SANITY-5-TC-001" }
```

The `doc_ingestor` stage picks these up automatically on the next run. To
process them on their own, from the repo root:

```bash
python scripts/generate_recorded_pages.py --profile <app>
```

This mechanically (no LLM) extracts every real locator from each recording
into `<workspace>/src/pages/recorded/Recorded<TestCaseID>Page.ts`, rewrites the
auto-generated section of `<workspace>/POM_SUMMARY.md` so the Coding Agent
knows these exist and reuses them instead of guessing, and folds each locator
into the ledger as a `recorded` card.

Login steps in a recording are intentionally skipped — the scaffold's
`src/fixtures/index.ts` `authenticatedPage` fixture already handles login.
