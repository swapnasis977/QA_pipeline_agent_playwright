Shared conventions for every agent. Prepended (cached) ahead of each agent's own
skill. Only rules that apply to more than one agent belong here; anything
agent-specific goes in skills/<agent>.skill.md.

## Output

- Respond with the JSON document only: no preamble, no fences, no sketch before
  the answer, one JSON block (the parser takes the LAST block that parses).
- Output that is cut off mid-JSON is wasted entirely. Prefer complete, concise
  entries over long prose in any field.

## Never fabricate

- If something required is missing (locator, field value, acceptance criterion),
  do not invent it. Flag it through your own skill's "unresolved"/"notes" mechanism
  so a human can supply the real value.
- Never put placeholders ("...", "TODO", "full POM content") in a field meant for
  real output: produce the real thing or flag the item unresolved.

## Test Case ID

- The Test Case Writer emits a sheet-local id only: "TC-<NNN>" (TC-001...). It must
  NOT add the story key; the pipeline prepends it ("PROJ-68308-TC-001"), and a
  writer-added prefix yields the doubled "PROJ-68308-PROJ-68308-TC-001".
- Every other agent reads the full id "<STORY_KEY>-TC-<NNN>" and uses it exactly as
  given: never invent, abbreviate, renumber or strip it. All downstream joins
  (utils/case_index.py) match this string.
- Every generated test title BEGINS with its full id, nothing before it
  (`test('PROJ-68308-TC-001 verifies ...')`). It is the only link between a
  Playwright report and a case: the Reviewer, Bug Reporter (screenshots) and
  Self-Healer (which file to patch) all resolve failures by it.

## Fixed framework scaffold (Planner, Coding Agent)

The workspace is scaffolded before any code is generated; a zero-LLM gate
(`verifiers/scaffold_check.py`) rejects any file outside this layout.

```
src/pages/        Page Objects      <Name>Page.ts, `extends BasePage`
src/components/   Component Objects <Name>.ts, `extends BaseComponent`
src/fixtures/     Fixtures          <name>.ts; `index.ts` exports { test, expect }
src/data/         Static test data
src/config/       `env.ts` — the ONLY place process.env is read
src/utils/        Helpers (`waits.ts` exists)
tests/ui|api|e2e/ Specs: tests/<ui|api|e2e>/<name>.spec.ts, matching the case's test_kind
```

- Paths are repo-relative and forward-slashed (a backslashed/absolute path is read
  by Playwright's CLI as a regex that matches nothing: 0 tests collected, "success").
- ALREADY EXIST — import, never redefine: `src/pages/BasePage.ts`,
  `src/components/BaseComponent.ts`, `src/fixtures/index.ts`, `src/config/env.ts`,
  `src/utils/waits.ts`.
- Aliases (tsconfig): `@pages/*`, `@components/*`, `@fixtures/*`, `@data/*`,
  `@utils/*`, `@config/*`.

## Locators live in page objects, never in a spec (Planner, Coding Agent)

- A `.spec.ts` has no raw locator (`page.getByRole(...)`, `page.locator(...)`,
  `page.getByTestId(...)`). Locators are named readonly properties of a page object
  under `src/pages/`; the spec calls those. A locator breaks because the APP
  changed, so the fix belongs where it is defined: behind a page object one fix
  serves every spec, instead of one heal per spec.
- If the page object you need does not exist, emit it as its own file in the same
  response. Sole tolerated exception: a one-off assertion target no other case
  needs and no page-object member covers (warning only).

## Framework reuse and credentials (Planner, Coding Agent)

- The repo's `src/pages/*.ts` and `src/fixtures/index.ts` are the ONE framework.
  Reuse what already covers part of a flow (most often login); never write an
  inline duplicate, even when DOM inspection came back empty. Guess only the
  genuinely new part.
- Never write a literal credential, API key or environment-specific URL/host, nor a
  fallback default for one (`process.env.X || 'admin'`). Generated TypeScript gets
  them from `import { ENV } from '@config/env'`, which reads APP_BASE_URL,
  APP_TEST_USER, APP_TEST_PASSWORD. A non-secret environment value (a tenant code)
  may keep a literal default, overridable by its own env var.
- `ENV`'s properties are camelCase, NOT the variable names. In full:
  `ENV.baseUrl`, `ENV.user`, `ENV.password`, `ENV.apiBaseUrl` (API_BASE_URL, else
  APP_BASE_URL), `ENV.headless`. Nothing else exists; `ENV.APP_BASE_URL` fails `tsc`
  and the file is rejected. Do not invent an accessor.
