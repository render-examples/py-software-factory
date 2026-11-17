# py-software-factory: implementation plan

Status: implemented locally; live Render/GitHub verification pending. Written 2026-11-13. Supersedes Phases 0
and 3 to 8 of `plan.md`. Decision and open-item IDs refer to `spec.md`.

## Approach

No spikes. Each open item is settled in one of two ways: by reading the pinned SDK sources (render 1.8.0 and
pydantic-ai-slim 2.68.0), or by choosing a design that works whichever way the
unknown falls. Whatever is still unconfirmed gets checked the first time a milestone runs on a deployed dev
instance, and `doctor` keeps checking it afterwards.

Work is ordered as vertical slices. Each milestone ends with something running on Render, starting with Triage
because it needs the least: one sandbox and one read-only, in-task agent.

## 1. Open items

| ID | What the source shows | Design that holds either way | First live check |
|---|---|---|---|
| O1 | Group credentials inject a header (`name`, optional `${SECRET}` template) for one exact domain, optionally limited by method and path. The source doesn't say whether a header the client already sent gets replaced. | The runner removes `x-api-key` from outgoing requests with an HTTP client hook, so the platform only ever adds a header. The runner trusts the system CA store (`truststore`) in case the injecting proxy presents its own CA. | `doctor` brokering probe (Milestone B) |
| O2 | Confirmed: snapshots belong to a group. `snapshots.list`, `delete`, and `from_id` take `sandbox_group_id`, and restoring works by snapshot name within the group. | One group, so nothing to change. | None needed |
| O3 | Credential create is in the low-level API only (`render.public_api.api.sandboxes.create_sandbox_group_credential`). | `setup-render` uses `RENDER_ADMIN_API_KEY` when set and falls back to `RENDER_API_KEY`. The admin key is never written to an env group. | Setup run (Milestone D) |
| O4 | Unknowable from source: it depends on the base image. | `install-tools.sh` installs a pinned `uv`, then a uv-managed CPython 3.12 into `/opt/factory/python` and the runner venv at `/opt/factory/venv`. The image's own Python is never used. | First snapshot build (Milestone A) |
| O5 | The factory owns the tools, so the shell policy runs before any workspace call, whichever placement the agent uses. | Closed by design. | None needed |
| O6 | `RenderSandbox` attaches to an existing sandbox through a `WorkspaceRef`. It neither paces requests nor retries, and lets `RateLimitError` propagate. | Each tool call first waits on a process-wide pacer, and retries on 429 or 503 with backoff. Fallback: a thin workspace backend over the factory's paced client, behind the same tools. | First Triage run (Milestone A) |

## 2. New decisions

These follow from the findings above. Each one is Proposed until reviewed.

| ID | Decision | Why |
|---|---|---|
| D23 | Long sandbox commands (agent run, install, verify, snapshot build) run detached: `sandbox/detach.sh` writes stdout, stderr, and the exit code to files, and the host polls with backoff from 2 to 10 seconds. Short commands use streaming exec. | The SDK's exec is a streaming request with a 45-second proxy read timeout and no exec timeout option. A 30-minute agent run can't depend on one long stream. |
| D25 | The runner is uploaded to each sandbox when it opens, as a hash-checked zip of `factory.runner`, `factory.agents`, and `factory.schemas`, and runs as `python -m factory.runner` with the zip on `PYTHONPATH`. Its dependencies are baked into the snapshot venv from `uv export --only-group runner`. | Code changes reach sandboxes with a Workflows deploy, as in the original. A dependency change changes the requirements hash recorded in the snapshot manifest, which triggers a rebuild. |
| D26 | The runner builds its Anthropic client with a placeholder key, a hook that removes `x-api-key`, and system trust. | Brokering then works whether or not the platform replaces existing headers (O1). |
| D28 | One process-wide pacer plus retries on 429 and 503 wraps every Sandbox API call: the factory's client, polling, and in-task tool calls. | The SDK doesn't retry or pace, and every workspace shares the 100 requests per minute limit. |
| D29 | One `Workflows()` app in `factory/workflows/app.py`, started with `render-workflows factory.workflows.app:app`. Tasks are registered with `retry=None` and take one dict input; results are JSON dicts. | These are the SDK's rules for registration and serialization. |

## 3. Milestones

### A. Triage on a deployed dev instance

Proves the sandbox lifecycle, the snapshot, the tools, and in-task placement.

| Module | Ported from | Notes |
|---|---|---|
| `factory/sandbox/client.py` | `client.ts` | Paced client over `RenderAsync().sandboxes`: waiting until running, collected exec with a 256 KB output tail and a timeout, detached exec (D23), upload, download, idempotent terminate, snapshot operations. |
| `factory/sandbox/egress.py` | `egress.ts` | `task_egress(repo, model_hosts)` and `builder_egress(repo)`. The builder adds the uv and PyPI hosts: `pypi.org`, `files.pythonhosted.org`, and GitHub's release-asset host. |
| `factory/sandbox/snapshot.py` | `snapshot.ts` | Builder sandbox in the factory group, tool install, runner user, clone with a staged token, scrub, install, lockdown, manifest with lockfile and runner-requirements hashes, snapshot, keep the newest two. The base-branch head comes from the GitHub API rather than `ls-remote`. |
| `factory/sandbox/open.py` | `sandbox.ts` | `open_sandbox(repo, ref, model_egress=...)` and `async with with_sandbox(...)`: restore by name (building inline once if none exists), verify, check the manifest, fetch the ref with a staged token, scrub, reinstall when lockfiles changed. |
| `sandbox/*.sh` | same | `install-tools.sh` (uv, Python, venv), `verify.sh` (non-dumpable probe dropped, Python version reported), new `detach.sh`. |
| `factory/agents/definition.py`, `providers.py`, `registry.py`, `prompts/*.md` | `define.ts`, `models.ts`, `registry.ts`, prompts | Dataclass definitions, the provider table (D16), four agents, prompts copied verbatim. |
| `factory/agents/shell_policy.py`, `tools.py` | `shell-policy.ts`, `runner/tools` | The five tools, written on `ctx.workspace.run`, with policy checks first, pacing, and `as-runner` wrapping when the agent is in-task. In-task commands run as root through the sandbox API, so every tool, including `write` and `edit`, goes through `as-runner` rather than the root file API. |
| `factory/agents/run.py` | `agents/run.ts` | `run_agent()` covering both placements. The in-task path uses the factory's paced workspace backend and maps `UsageLimitExceeded`, `RunTimedOut`, and `UnexpectedModelBehavior` to `AgentError` codes. Validates the output a second time and emits `agent.finished`. |
| `factory/stages/triage.py`, `thread.py`, `spec.py` | `workflows/triage/*` | Plain async stage functions that take injected dependencies. |
| `factory/workflows/app.py`, `github_task.py`, `triage.py` | `github-task.ts`, `entrypoints/workflows.ts` | `define_github_task`: validate the input, resolve the repository, `with_claim` (in continue mode when the run has a parent). |
| `render.yaml` | same | Python runtime, `uv sync --frozen` build, the same env groups. Until Milestone D, env groups are created by hand. |

Tests: pacing, retries, and detached exec against a fake SDK; egress policies; snapshot and open steps at their
security boundaries (token staged then scrubbed, commands run as the runner user, fetch refs validated); shell
policy; tools against `LocalWorkspace` in a temp directory; a triage agent scripted with `FunctionModel`; the
triage stage ported from `triage.test.ts`.

Exit: applying `factory:triage` on the example target produces a spec or questions, and the first live run
confirms O4 and O6.

### B. Spec-to-PR, with the Implementer in the sandbox

Proves the runner, wire protocol v3, and brokering.

| Module | Ported from | Notes |
|---|---|---|
| `factory/runner/protocol.py` | `runner/core/protocol.ts` | Pydantic models for `RunnerInput` and `RunnerEvent`, plus exit codes. Protocol version 3: no key on stdin. |
| `factory/runner/main.py`, `model.py` | `runner/main.ts`, `kernel.ts` | Validates stdin, builds the agent from the registry, `LocalWorkspace` on the repository, request limit and timeout, an event handler writing JSON Lines, exceptions mapped to error events. `model.py` implements D26. |
| `factory/agents/run.py` (in-sandbox path) | `agents/run.ts` | Uploads the input, runs `run-agent.sh` detached, downloads the JSON Lines, summarizes, validates again. |
| `factory/sandbox/credentials.py` | new | Group credential create and update through the low-level API, used by setup and `doctor`. |
| `factory/stages/spec_to_pr.py`, `verify.py`, `branch.py`, `messages.py` | `workflows/spec-to-pr/*`, `run-verify.ts` | Approval (already ported), implement, verify (detached), bounded repair, publish (already ported), draft PR. |
| `sandbox/run-agent.sh` | same | Key handling removed; runs the runner from the venv as the runner user. |

Tests: runner tests through the real entry point with `FunctionModel`, covering the result, the error codes, and
the event stream; the in-sandbox `run_agent` against a fake sandbox (runner lost, invalid lines, rejected
output); invariant 12, asserting that no run input, uploaded file, or command contains a model key; the Spec-to-PR
stage ported from `spec-to-pr.test.ts`.

Exit: `factory:spec-approved` produces a draft PR, and the `doctor` brokering probe passes (O1).

### C. PR Review, Cancel, and snapshot refresh

| Module | Ported from |
|---|---|
| `factory/stages/pr_review.py`, `diff_lines.py`, `post.py` | `workflows/pr-review/*` |
| `factory/workflows/cancel.py` | `workflows/cancel.ts`; task run control is done |
| `factory/workflows/refresh_snapshot.py` | `workflows/refresh-snapshot.ts` |

Exit (Phase 5's exit in `plan.md`): each label produces the expected GitHub result, and `/factory cancel` stops a
run and terminates its sandbox.

### D. Setup, doctor, docs, and the deploy button

Console scripts in `scripts/`: `setup-app` (App manifest flow), `setup-render` (env groups, the sandbox group, one
credential per provider, `FACTORY_SANDBOX_GROUP_ID` written to the Workflows group), `setup-github` (webhook and
labels), `preflight`, and `doctor` (adds checks for the group, the credential, and brokering). Port the script
tests. Docs: quickstart, configuration, architecture, setup runbook, extension runbook ("choose a placement").

Exit: a fresh fork deploys by following only the setup runbook, and `doctor` passes.

### E. Autopilot as a parent task

`factory/workflows/autopilot.py` ports the original task-parent design. It claims the issue, then calls Triage,
Spec-to-PR, and PR Review as child tasks with `await ctx.run(...)`. Child results, not GitHub labels, advance the
pipeline. A child continues the root claim instead of replacing it. When Triage asks questions, Autopilot adds
its label back and ends normally; a human reply starts a new parent task.

Tests port `autopilot.test.ts`: stage order, handoffs, stopping on each child outcome, the questions path,
claim ownership across child tasks, and cancellation of the root task plus its recorded sandbox.

Exit: an Autopilot task reaches a reviewed draft PR.

### F. Webinar readiness

Unchanged from `plan.md` Phase 8: re-pin and re-test, the placement-switch demo, a dry run of fork-and-deploy,
and known limits in the README.

## 4. Risks

- **Rate limit.** Each in-task tool call is at least one request, and detached polls add more. Three concurrent
  runs could hit 100 requests a minute. D28 paces and retries; `agent.finished` should record the sandbox
  request count so the cost is visible.
- **Two HTTP stacks.** pydantic-ai-slim depends on `httpx2` while the `anthropic` SDK uses `httpx`. The D26 hook
  has to go on the client the Anthropic SDK actually uses, and the runner tests should assert that.
- **Egress host list for the builder.** uv and Python downloads may redirect to hosts that aren't on the
  allow-list. The builder's failure message names the blocked host, as `egressHint` does in the original.

## Out of scope

Unchanged: the Postgres spend ledger and triage conversation carry-over. Flows are deliberately outside the
factory's scope.
