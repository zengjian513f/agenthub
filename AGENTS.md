# AgentHub project guidance

## Keep production deployments in sync

- The production hub is `https://hub.example.com/agenthub/`. It serves its own
  frontend; updating NodeA or NodeB alone does not update this website. The same
  repository also provides both nodes' independently accessible local Web UI.
- Unless the user explicitly limits a task to design, review, or local work,
  completing a production-facing fix includes validation, committing and pushing
  the intended changes to GitHub, deploying to every affected target, and checking
  the running result. This is standing authorization for routine updates; do not
  ask again for deployment permission already covered by the task.
- Shared frontend/assets and shared server/API/protocol changes must reach the
  central hub, NodeA, and NodeB. For hub-only or node-only code, inspect imports and
  request paths before choosing the affected services. Never treat a local browser
  check or a successful `git push` as evidence that hub-host has been deployed.
- Follow [the deployment runbook](docs/deployment.md). Inspect remote revisions
  and worktrees first, preserve concurrent changes, and publish the intended
  committed files and assets. Do not discard remote changes or force-push.
- Restart only affected Web services. Preserve existing tmux/CLI sessions,
  queues, node identities, credentials, and hub registry. Keep the existing public
  login protection and server-side-only machine administration.
- Documentation/test-only changes need GitHub and checkout/file synchronization,
  but no Web restart. Inspect the full gap from each deployed revision: an earlier
  undeployed runtime change still requires deployment and appropriate validation.
- Check service health and the relevant production browser/API behavior; retain
  standalone node access. Report the commit and deployed targets, or state the
  specific target and blocker when publication could not finish. Do not claim
  completion based solely on a local edit, test, or commit.

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

- `tests/claude_monkey.py` and the non-`--simulate` mode of
  `tests/dual_cli_monkey.py` are explicit, paid integration tests. Never run
  either as part of the normal test suite or without the user's authorization.
- `tests/dual_cli_monkey.py --simulate` is a free scheduler/model check: it must
  not start tmux, a browser, Claude, Codex, or contact the agenthub service.
- When it is authorized, use the full dated Haiku model ID and keep the
  JSONL model assertion enabled. Do not use the `haiku` alias and do not fall
  back to a more expensive model.
