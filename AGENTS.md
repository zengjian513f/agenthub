# Sesman project guidance

## Diagnose browser-visible bugs before changing code

- Reproduce a reported UI, rendering, queue, or terminal interaction bug with
  Playwright/headless Chromium against the exact session named by the user
  before implementing a fix. Inspect the rendered DOM and relevant browser
  state as well as the API and native JSONL; API/JSONL evidence alone is not a
  browser reproduction.
- A headless browser has isolated storage. If the issue exists only in the
  user's original tab, distinguish browser-local state from server/session
  state and inspect or export that tab's state instead of guessing.
- Re-run the same browser scenario after the change. Do not turn a missing
  reproduction into cleanup logic that merely hides the symptom.

## Paid CLI tests

- `tests/claude_monkey.py` is an explicit, paid integration test. Never run it
  as part of the normal test suite or without the user's authorization.
- When it is authorized, use the full dated Haiku model ID and keep the
  JSONL model assertion enabled. Do not use the `haiku` alias and do not fall
  back to a more expensive model.
