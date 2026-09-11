# AgentHub project guidance

## Keep production deployments in sync

- Read the untracked `DEPLOYMENT.local.md` for actual hosts, users, paths, and
  network settings before deployment. Public documentation contains examples,
  not production targets; never deploy to the example addresses. If local details
  are missing, obtain the target information instead of guessing.
- The central hub serves its own frontend; updating a node alone does not update
  it. The same repository also provides independently accessible local Web UIs.
- Unless the user explicitly limits a task to design, review, or local work,
  completing a production-facing fix includes validation, committing and pushing
  the intended changes to GitHub, deploying to every affected target, and checking
  the running result. This is standing authorization for routine updates; do not
  ask again for deployment permission already covered by the task.
- Shared frontend/assets and shared server/API/protocol changes must reach the
  central hub and all deployed nodes. For hub-only or node-only code, inspect imports and
  request paths before choosing the affected services. Never treat a local browser
  check or a successful `git push` as evidence that the central hub has been deployed.
- Follow [the deployment runbook](docs/deployment.md). Inspect remote revisions
  and worktrees first, preserve concurrent changes, and publish the intended
  committed files and assets. Do not discard remote changes. History rewriting
  requires user authorization, a private backup, and explicit per-branch leases;
  it is not part of routine deployment.
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

## Keep deployment and identity information private

- Never commit `DEPLOYMENT.local.md`, real deployment addresses, SSH usernames,
  personal filesystem paths, credentials, or copies of local runtime data.
  Use documentation IP ranges, example domains, and user-relative paths in examples.
- Keep actual deployment settings in ignored local files and installed service
  overrides. Preserve them during updates; never replace them with public examples.
- Use the repository owner's GitHub noreply email for new commits. Before making
  a repository public, inspect all published branches and history for credentials
  and private metadata. Never publish local backup branches or history bundles.

## Diagnose browser-visible bugs before changing code

- Always keep the console button visible, including loading, unavailable nodes,
  and subagent views. When unavailable, show a gray button (no exclamation badge),
  explain the reason in a toast on hover/focus, and show the specific error on
  click. Keep it clickable; never hide it or use the native disabled attribute
  to prevent access to the explanation.

- Offline machine filters use pale text in the light theme, whether selected or
  unselected. Hover must not change their appearance or show a toast. Clicking
  only opens the specific error dialog; neither clicking nor double-clicking
  may change selection. Do not show a separate offline warning banner.

- Diagnose from the captured evidence first. A bug report bundle carries the
  user's screenshots, `browser-state.json`, `events.jsonl` and the audit
  store, and the server records every event anyway; read those before
  touching code. Read the DOM, browser state, HTTP/SSE audit, server ledgers,
  tmux/ptyhost screens and native JSONL together, and find the first event in
  that chain that disagrees with expectation.
- Reproduce with Playwright/headless Chromium when it is cheap and the
  evidence leaves a real gap; it is a tool, not a gate. Intermittent bugs
  often will not reproduce on demand: do not spend the task forcing one, and
  do not build a reproduction framework before diagnosing.
- A headless browser has isolated storage. If the issue exists only in the
  user's original tab, distinguish browser-local state from server/session
  state and inspect or export that tab's state instead of guessing.
- Verify the fix against the failure's shape: a regression test that fails on
  the pre-fix commit, or the same browser scenario when one was reproduced.
  Do not turn a missing reproduction into cleanup logic that merely hides the
  symptom.

## Paid CLI tests

- `tests/claude_monkey.py` and the non-`--simulate` mode of
  `tests/dual_cli_monkey.py` start real CLI sessions and cost money. They are
  explicit standalone runs: never wire them into unittest/e2e discovery or the
  normal test suite. Running them when the task calls for it needs no separate
  authorization; the only hard rule is the model.
- Paid runs must use the cheapest model at low effort: Claude Code
  `claude-haiku-4-5-20251001` with `--effort low`, Codex `gpt-5.6-luna` with
  `model_reasoning_effort="low"`, Grok `grok-4.6` at low effort. Use the full
  dated Haiku model ID (never the `haiku` alias), keep the JSONL model
  assertion enabled, and never fall back to a more expensive model.
- `tests/dual_cli_monkey.py --simulate` is a free scheduler/model check: it must
  not start tmux, a browser, Claude, Codex, or contact the agenthub service.
