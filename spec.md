# py-software-factory: specification

Status: draft for review. Written 2026-11-13.

## 1. Summary

`py-software-factory` is a Python port of
[render-examples/software-factory](https://github.com/render-examples/software-factory),
adapted to use Pydantic AI as the agent loop. It is the starting point for a
webinar on building a software factory on Render.

The port keeps the original's security model and GitHub-native workflow. It
changes four things:

1. The agent loop is Pydantic AI, called directly. The generic harness adapter
   layer is dropped.
2. Agents run in one of two placements: inside the sandbox, or inside the
   Workflows task with tools executing in the sandbox.
3. The model key is brokered by Render sandbox group credentials and never
   enters a sandbox.
4. Autopilot is a Render Workflows flow, so a lost instance replays without
   repeating finished stages.

### Baseline versions

These are the versions this spec was written against. Pin them.

| Component | Version | Notes |
|---|---|---|
| software-factory (TS) | commit `fd3731d`, 2026-11-06 | Source of the port |
| pydantic-ai | 2.68.0 (2026-11-12) | Requires Python 3.11+; ships several times a week |
| pydantic-ai-harness | 0.x | API may change between minor releases |
| render (Python SDK) | 1.8.0 | Sandbox client is under `render.experimental` |

## 2. Goals and non-goals

### Goals

- Show two agent placements, agent-in-sandbox and agent-in-task, from one
  agent definition.
- Keep fork-and-deploy cheap: two services, no database.
- Preserve every security invariant of the original (section 9).
- Show patterns that transfer to other agent libraries: placement, typed
  submission, host-owned writes, and the host-to-sandbox wire protocol.

### Non-goals

- A beat-for-beat port. Security-bearing code is ported faithfully; the rest
  is adapted.
- Supporting more than one agent library in this repo. The TS repo remains
  the multi-harness reference.
- Postgres, a cross-run spend ledger, or per-model-request durability
  (section 11).

## 3. Decision log

"Decided" means Jacob stated or confirmed it. "Proposed" means it was
recommended in discussion and not explicitly confirmed; treat these as the
default unless changed in review.

| ID | Decision | Status |
|---|---|---|
| D1 | Port the TS repo to Python; do not build a polyglot bridge | Decided |
| D2 | Name the project `py-software-factory` | Decided |
| D3 | Drop the generic harness interface (`Harness`, `Driver`, Pi driver); call Pydantic AI directly | Decided |
| D4 | No Postgres in the base repo; it is a follow-up | Decided |
| D5 | Split placement: some agents in-sandbox, some in-task | Decided |
| D6 | Port all setup scripts, including `doctor` | Decided |
| D7 | Gateway uses FastAPI | Decided |
| D8 | Model key sits behind credential brokering | Decided |
| D9 | Tasks for single stages; Autopilot as a flow, added last | Decided (recommendation accepted) |
| D10 | Do not use the Pydantic AI `RenderWorkflows` integration on the main path | Proposed |
| D11 | Replace the kernel and `submit` tool with Pydantic AI `output_type`, request limits and run timeout | Proposed |
| D12 | Keep a host-to-sandbox wire protocol as the trust boundary | Proposed |
| D13 | The factory owns its five tools, written once against Pydantic AI's workspace | Proposed |
| D14 | Port the read-only shell policy | Proposed |
| D15 | Package the runner as a pinned virtualenv baked into the snapshot | Proposed |
| D16 | Model config is a model string plus a provider table (key variable, API host) | Proposed |
| D17 | Replace the harness conformance suite with `FunctionModel`-scripted runner tests | Proposed |
| D18 | Tooling: uv, ruff, pyright, pytest; typed Python config module | Proposed |
| D19 | One sandbox group; the egress allow-list decides which sandboxes can use the brokered key | Proposed |
| D20 | GitHub fetch tokens are not brokered; keep stage-then-scrub | Proposed |
| D21 | Default placement: Triage and Spec Writer in-task; Implementer and PR Reviewer in-sandbox | Proposed (split is decided; this assignment was the recommendation) |
| D22 | Keep one telemetry emit function and an optional storage argument on agent runs, so follow-ups slot in | Proposed |

### Rationale for the decisions that shaped the design

**D3, drop the harness interface.** The adapter layer exists so a second
library can sit behind the kernel. With one library, Pydantic AI supplies the
abstraction: models are swappable by string, and the kernel's duties map to
library features (D11).

**D4, no Postgres.** The original runs without a database by design and its
architecture doc argues against one for claims, because the GitHub status
comment already owns them. Leaving it out costs three things the original
also lacks: a cross-run dollar budget, resume after a crash, and conversation
carry-over in triage. D9 covers resume for Autopilot.

**D8, credential brokering.** The original uploads the model key into the
sandbox and protects it with a root-only file, a key-on-stdin handoff, env
scrubbing and an execute-only interpreter. Brokering removes all of that and
the "model key exists in runner memory" residual risk.

**D9, tasks and one flow.** Flows journal steps and replay after an
infrastructure failure. A single stage is one agent run, so replay buys
little there. Autopilot is the longest and most expensive run, so that is
where it pays. Flows cannot call tasks, so the flow runs stages inline and
gives up per-stage compute.

**D10, skip the `RenderWorkflows` integration.** It chains each model request
and tool call as a task but does not checkpoint, and the factory disables
retries to avoid repeating spend. Its generated tasks can also be submitted
directly by anyone with Workflow API access, bypassing the per-run permission
check.

## 4. Architecture

```text
GitHub --signed webhook--> Gateway (FastAPI)
Gateway --start task / flow, delivery ID as idempotency key--> Workflows (Python)
Workflows --restore snapshot, exec--> Sandbox
Sandbox --typed result or candidate tree--> Workflows
Workflows --validated write--> GitHub
```

The Blueprint deploys two services, as in the original:

- **Gateway.** Verifies webhook signatures, maps each delivery to one task or
  flow run. Holds no GitHub credential and no model key.
- **Workflows.** Registers tasks and the Autopilot flow, manages sandboxes,
  performs every GitHub write. Holds the GitHub App credentials, the Render
  API key and the model key (for in-task agents only).

State ownership is unchanged: Render Workflows owns run status; GitHub owns
issues, labels, specs, pull requests and reviews; the App-authored status
comment owns the claim on an issue or pull request; sandbox snapshots hold
the prepared repository; telemetry is JSON lines in logs.

### Proposed layout

```text
py-software-factory/
  pyproject.toml  uv.lock  render.yaml  factory_config.py  .env.example
  factory/
    gateway/      signature, body limit, routes
    github/       app auth, request, client, git data, markers
    claims/       permission, status comment, claim lifecycle
    changeset/    candidate tree parsing and publishing
    format/       text safety, spec, review, pull request bodies
    sandbox/      paced client, open/restore, snapshot, egress, credentials
    agents/       definitions, prompts, tools, shell policy, models
    runner/       in-sandbox entry point and wire protocol
    stages/       plain stage functions shared by tasks and the flow
    workflows/    task and flow registration, cancel, snapshot refresh
    schemas/      Pydantic models for outputs and task inputs
    config.py  events.py
  sandbox/        shell scripts (ported from the original)
  scripts/        setup-app, setup-render, setup-github, preflight, doctor
  tests/
  docs/           quickstart, configuration, architecture, runbooks
```

## 5. Agents

### Definition

An agent definition fixes a stable ID, prompt file, tool set, output model,
turn limit, model, and placement. It is a small dataclass that builds a
Pydantic AI `Agent`. Prompts are copied verbatim from the original.

| Agent | Tools | Turn limit | Model tier | Placement (D21) |
|---|---|---|---|---|
| Triage | read, ls, bash | 40 | Sonnet | In-task |
| Spec Writer | read, ls, bash | 60 | Opus | In-task |
| Implementer | read, ls, bash, edit, write | 150 | Opus | In-sandbox |
| PR Reviewer | read, ls, bash | 100 | Sonnet | In-sandbox |

### Kernel replacement (D11)

| Original kernel duty | Replacement |
|---|---|
| Typed `submit` tool, validated against JSON Schema | `output_type` with a strict, bounded Pydantic model |
| `max_turns` | `UsageLimits(request_limit=...)` |
| `timeout_ms` | run `timeout=` |
| Protocol events | `run_stream_events()` or an event handler |
| Usage and cost | run usage; optional per-run `cost_limit` |

The host validates the output a second time against the same Pydantic model,
as the original does with Zod.

### Tools (D13, D14)

The factory defines `read`, `ls`, `bash`, `edit` and `write` once, against
Pydantic AI's workspace API (`ctx.workspace`). The same definitions run with
a local workspace inside the sandbox and with `RenderSandbox` from a task.
The Harness's own `Shell`, `FileSystem` and `Coder` capabilities are not
offered to models.

Per-agent tool authority stays declared in the definition and pinned by
tests. Agents without `edit` or `write` are read-only, and `bash` applies the
ported shell policy. As in the original, the policy is guidance, not a
security boundary.

### Model configuration (D16)

Each agent names a Pydantic AI model string. A small provider table maps each
provider to its key environment variable and API hostname. The hostname feeds
the egress allow-list and the brokered credential; the variable is read by
the Workflows service for in-task agents and by setup for the credential.

## 6. Placement

### Agent-in-sandbox

The host writes the run input into the sandbox and executes the runner once.
The runner builds the agent, runs the loop with a local workspace, and writes
JSON Lines events. One exec call covers the whole run, and tool calls are
local.

- Package: `pydantic-ai-slim`, installed in the snapshot's virtualenv (D15).
- Model access: through the brokered credential (section 7). The runner uses
  a placeholder key.
- Egress: the model API host is on the sandbox's allow-list.

### Agent-in-task

The Workflows task opens the sandbox as the original does (restore, fetch,
scrub credentials), then runs the agent loop in the task process. Tools
execute in that sandbox through `RenderSandbox` attached by sandbox ID.

- Package: `pydantic-ai-harness`, for `RenderSandbox` only.
- Model access: the key is in the Workflows service environment and never
  enters the sandbox.
- Egress: the model API host is not on the sandbox's allow-list.
- Cost: each tool call is at least one Sandbox API request, against the
  workspace limit of 100 requests per minute.

### Wire protocol (D12)

The in-sandbox path keeps the original's contract: a JSON run input on stdin,
JSON Lines events on stdout ending in exactly one `result` or `error`, and
the same exit codes. Field shapes match the TS protocol.

One intentional difference: the model key is no longer the first line of
stdin, because of brokering. This is an incompatible change, so the protocol
version is 3. This departs from the earlier aim of matching the TS shapes
exactly.

## 7. Credential brokering (D8, D19, D20)

Render sandbox groups hold credentials that the platform injects into
matching outbound requests. Verified in the Python SDK source (1.8.0); not
yet exercised in a live sandbox.

- **Setup** creates one factory sandbox group and one credential per model
  provider. For Anthropic: domain `api.anthropic.com`, header injection of
  `x-api-key`, restricted to `POST /v1/messages`.
- **Runtime** creates every sandbox in that group.
- **Gating** is by egress. In-sandbox agent runs include the model host on
  the allow-list. The snapshot builder and in-task sandboxes omit it, so the
  credential cannot be used from them.
- **GitHub fetch tokens** are short-lived and per-installation, which does
  not fit a static, admin-managed group credential. The stage-then-scrub
  scripts are ported unchanged.

Platform limits: 25 credentials per group; exact public hostnames only;
create and update are admin-only; the API never returns secret values. The
call is in the generated low-level API (`create_sandbox_group_credential`),
not the high-level sandbox client.

Brokering stops the key being stolen, not used. Code in a sandbox can still
spend through it while the sandbox lives, so the method and path restriction
and the turn limits still matter.

### Removed from the port as a result

- Key upload and the root-only key file in `run-agent.sh`.
- The key-on-first-line-of-stdin handoff.
- Key scrubbing from tool environments.
- The execute-only interpreter and the non-dumpable probe in `verify.sh`.

## 8. Tasks and flows (D9)

Stage logic lives in plain functions under `factory/stages/`. Tasks and the
flow both call them.

| Run | Kind | Notes |
|---|---|---|
| Triage | Task | Label-triggered single stage |
| Spec-to-PR | Task | Label-triggered single stage |
| PR Review | Task | Label-triggered single stage |
| Cancel | Task | Stops the claimed run and terminates its sandbox |
| Refresh snapshot | Task | Runs on base-branch push |
| Autopilot | Flow | Steps: triage, implement, verify, repair, publish, review |

Rules for the flow, from the flows docs and SDK:

- Flow code must be deterministic. All I/O, including claim checks and GitHub
  reads, goes inside steps.
- Step functions are sync and receive only JSON arguments. Each step rebuilds
  its sandbox handle and GitHub client from IDs, and wraps async code.
- The sandbox ID is a step result, so a replay reattaches to the same sandbox.
- An interrupted step reruns in full. Publishing is already safe to repeat
  (deterministic branch, hidden markers, tree SHA check); an interrupted
  agent step repeats that step's spend.
- Replay covers infrastructure failures only. Out-of-memory, timeouts and
  uncaught errors fail the run.
- There is no wait primitive. When Triage asks questions, the flow ends and a
  human reply starts a new run, as today.
- Replays keep the flow run ID, so the claim in the status comment survives.
  Run control must read and cancel both task runs and flow runs.

Task retries stay disabled for GitHub tasks, as in the original.

## 9. Security invariants

Carried over from the original. Tests must pin them.

1. Verify a webhook signature before parsing its body.
2. Check the triggering actor's current repository permission for every
   GitHub task or flow run.
3. Only code in the Workflows service writes to GitHub.
4. GitHub write credentials never enter a sandbox.
5. Every model-directed tool runs inside a sandbox. This holds for both
   placements: in-task agents run the loop on the host, but their tools
   execute in the sandbox.
6. Repositories, refs, branches and write targets come from trusted input,
   never model output.
7. Publish only after GitHub reproduces the validated tree SHA.
8. Automation stops at a draft pull request and a COMMENT review.
9. Implement the specification as it existed when approved.
10. Every sandbox has a provider timeout and `finally` cleanup. A GitHub
    run's sandbox is recorded so cancellation can terminate it.
11. Every sandbox is created with an egress allow-list built from trusted
    configuration.

New in this port:

12. The model key never enters a sandbox, in either placement.

## 10. Port inventory

Sizes are lines in the TS repo.

| Area | Size | Fidelity | Notes |
|---|---|---|---|
| Gateway | 259 | Faithful | FastAPI; raw body read before parsing |
| GitHub client | 1,007 | Faithful | App auth, tokens, retries, Git Data API, markers |
| Claims | 548 | Faithful | Extended to cover flow runs |
| Changeset publishing | 407 | Faithful | Byte-level parsing and validation |
| Formatting | 376 | Faithful | Marker neutralizing, mention escaping |
| Workflows | 1,253 | Faithful logic | Restructured into stage functions, tasks, one flow |
| Sandbox lifecycle | 954 | Adapted | Python SDK; sandbox group; Node-specific parts replaced |
| Sandbox shell scripts | 434 | Mostly kept | Changes in `install-tools.sh`, `run-agent.sh`, `verify.sh` |
| Schemas | 240 | Adapted | Strict, bounded Pydantic models |
| Agents and prompts | 364 | Adapted | Prompts verbatim |
| Runner tools and shell policy | 392 | Adapted | Rewritten on the workspace API; policy ported |
| Config and events | ~160 | Adapted | |
| Setup scripts | 1,479 | Faithful | Adds sandbox group and credential setup |
| Tests | 5,972 | Faithful for security; adapted elsewhere | pytest, same fakes |
| Blueprint, CI, docs, runbooks | n/a | Rewritten | Python runtime |

Not ported: `app/harness`, the `Driver` contract, the Pi driver, the kernel,
`submit`, the esbuild runner bundle, and the key-handling code listed in
section 7 (about 700 lines of TypeScript, plus the key steps in the shell
scripts).

## 11. Follow-ups, out of scope

- **Postgres spend ledger.** A cross-run dollar budget, checked before each
  run. D22 keeps the emit point ready for it.
- **Triage conversation carry-over.** Continue a saved conversation when a
  human replies, using the optional storage argument from D22.
- **Flows durability backend for Pydantic AI.** Journal each model request
  and tool call as a flow step. This would address the checkpointing gap in
  the current `RenderWorkflows` integration.

## 12. Open items

These are unverified. The plan's first phase resolves them, and each has a
fallback there.

| ID | Question | Affects |
|---|---|---|
| O1 | Does injection replace a header the client already sent, and does the model SDK work with a placeholder key under the platform's TLS handling? | D8 |
| O2 | Are snapshots scoped to a sandbox group? | D19 |
| O3 | Does setup need an admin API key for credential creation while runtime uses a narrower one? | Setup scripts |
| O4 | Is the sandbox image's Python 3.11 or later, and can a virtualenv be baked into a snapshot and restored intact? | D15 |
| O5 | Can the shell policy and write restrictions be enforced through the workspace API in both placements? | D13, D14 |
| O6 | Does `RenderSandbox` attach cleanly to a host-opened sandbox, and can its calls be paced under the 100 requests/minute limit? | In-task placement |
| O7 | Do flows behave as documented for this use: sync steps wrapping async code, cancel, replay with sandbox reattach? | D9 |

Confirmed from SDK source, not from a running system: the Python SDK has
snapshots, egress policies, subtask calls, idempotency keys on task and flow
starts, and task and flow run cancellation.

## 13. Risks

- **Release pace.** Pydantic AI ships several times a week and the Harness is
  0.x. Pin both and re-test shortly before the webinar.
- **New platform features.** Flows are marked "New" and have "not supported
  yet" paths (invoking tasks, sleep, async steps). The sandbox client is
  under `experimental`.
- **Faithful ports of security code.** Most of the repo is plumbing where the
  invariants live. A loose port can lose one silently, so those modules are
  ported with their tests first.
- **Rate limits.** Both services and every in-task tool call share one
  workspace limit for the Sandbox API.

## 14. Sources

- [render-examples/software-factory](https://github.com/render-examples/software-factory)
- [Pydantic AI: Agent](https://pydantic.dev/docs/ai/core-concepts/agent/index.md)
- [Pydantic AI: Render Sandbox](https://pydantic.dev/docs/ai/harness/render-sandbox/index.md)
- [Pydantic AI: Render Workflows](https://pydantic.dev/docs/ai/harness/render-workflows/)
- [Pydantic AI: Durable Execution overview](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/index.md)
- [Render docs: Defining Flows](https://render.com/docs/workflows-defining-flows)
- `render` Python SDK 1.8.0, read from the published wheel
