# py-software-factory: implementation plan

Status: draft for review. Written 2026-11-13. Read `spec.md` first; decision
and open-item IDs (D1, O1) refer to it.

## Approach

1. **Spikes first.** Seven open items could change the design. Resolve them
   in throwaway code before porting anything that depends on them.
2. **Security-bearing code next, with its tests.** These modules do not
   depend on the agent design, so they can start in parallel with the spikes.
3. **Agents, then stages, then deployment.** One working stage end to end
   before the others.
4. **The flow last.** It reuses the stage functions and is the newest
   platform surface.

Sizes below are lines in the TS repo, as a guide to relative effort. No
durations are given: they depend on who does the work and how the spikes
land.

## Phase 0: spikes

Each spike is a small script against a real Render workspace. Each has an
exit question and a fallback if the answer is no.

| Spike | Resolves | Exit question | Fallback |
|---|---|---|---|
| S1 Credential injection | O1, O3 | Does a model call from a sandbox succeed with a placeholder key, and is a client-sent key header replaced? | Port the original key handling (key on stdin, env scrubbing, non-dumpable interpreter) and record the residual risk |
| S2 Snapshot and group scope | O2 | Can a sandbox in the factory group restore the factory's snapshot? | If two groups were wanted later, build snapshots per group; one group needs no change |
| S3 Python in the sandbox | O4 | Is Python 3.11+ present, and does a virtualenv survive snapshot and restore? | Install Python in `install-tools.sh`, or ship the runner as a single-file zipapp with vendored dependencies |
| S4 Tools on the workspace API | O5 | Do the five tools and the shell policy work unchanged with a local workspace and with `RenderSandbox`? | Two thin tool backends behind one declared tool list: local in the sandbox, sandbox-exec from the task |
| S5 In-task attach and pacing | O6 | Does `RenderSandbox` attach to a host-opened sandbox by ID, and can calls be paced and retried on 429? | Skip `RenderSandbox`; back the in-task tools with the factory's own paced sandbox client |
| S6 Flow behavior | O7 | Does a flow with sync steps wrapping async code replay correctly after a killed instance, reattach to its sandbox, and cancel cleanly? | Autopilot stays a task parent calling child tasks, as in the original |
| S7 Task basics | Confirms SDK reading | Do subtask calls, idempotency keys and run cancellation behave as the SDK source suggests? | None expected; adjust call sites |

S1 and S6 create billable resources (a sandbox group, a credential, sandboxes,
workflow runs). Run them in a dedicated workspace.

**Exit:** each open item in the spec is marked resolved, with the fallback
taken where needed, and the spec is updated.

## Phase 1: scaffold

- Repository with `pyproject.toml`, `uv.lock`, ruff, pyright, pytest, and a
  single `check` command.
- CI running `check`.
- `factory_config.py` with a typed, validated config (repos, labels, command
  prefix, minimum permission, limits, sandbox timeout, egress hosts).
- `factory/config.py` (repo resolution, task and flow names) and
  `factory/events.py` (one JSON-lines emit function, per D22).
- Pinned versions from the spec's baseline table.

**Exit:** `check` passes on an empty skeleton; an invalid config fails at
import.

## Phase 2: security-bearing core

Faithful ports, each with its tests ported alongside. No dependency on the
agent design, so this can run in parallel with Phase 0.

| Module | Size | Must preserve |
|---|---|---|
| `gateway/` | 259 | Signature verified on the raw body before parsing; body size limit; routing is a pure function from delivery to task input; delivery ID as idempotency key |
| `github/` | 1,007 | App JWT and installation tokens; retries on 5xx and rate limits with a wait cap; Git Data calls stay on the host; hidden markers |
| `format/` | 376 | Marker neutralizing; mention escaping; body length limits |
| `schemas/` | 240 | Every output model strict and bounded |
| `claims/` | 548 | Permission check per run; status-comment ownership; `assert_current` before durable writes; recorded sandbox ID |
| `changeset/` | 407 | Strict parsing of the changes file; path, mode, count and size checks; reject workflow files, symlinks and submodules; tree SHA comparison |

**Exit:** ported tests for invariants 1 to 4 and 6 to 9 pass, using fakes
only (no GitHub, Render or model calls).

## Phase 3: sandboxes

- Paced sandbox client on the Python SDK: request pacing, retries on 429 and
  503, wait-until-running, exec output collection.
- `open_sandbox` and `with_sandbox`: restore, verify, fetch the exact ref
  with a temporary read-only token, scrub it, reinstall on lockfile change,
  close in `finally`.
- Snapshot build and refresh, keeping the newest two per repository.
- Egress policy builders: task sandboxes with and without the model host,
  and the snapshot builder (adds the PyPI hosts).
- Sandbox group and credential management, per S1 and S2.
- Shell scripts: copy from the original; change `install-tools.sh` (Python
  and virtualenv), `run-agent.sh` (no key handling) and `verify.sh` (no
  non-dumpable probe), per S1 and S3.

**Exit:** a script opens a sandbox from a snapshot for the example target
repository, runs the verify commands, and closes it. Tests pin invariants 10
and 11.

## Phase 4: agents

- Agent definition dataclass and registry; four definitions with prompts
  copied verbatim.
- Provider table and model strings (D16).
- The five tools and the shell policy, per S4.
- In-sandbox runner: wire protocol v3 as Pydantic models, entry point,
  self-test, event emission, exit codes.
- Host-side `run_agent` with both placements behind one call, second
  validation of the output, and `agent.finished` telemetry.
- In-task path with `RenderSandbox` attach, per S5.
- Runner tests scripted with `FunctionModel` (D17); authority tests pinning
  which agents can write.

**Exit:** the same test agent returns a valid typed result in both
placements against a real sandbox, with invariant 12 checked (no model key
in the sandbox's environment, files or run input).

## Phase 5: stages and tasks

Order matters here: finish Triage end to end before starting the others.

1. `stages/triage` and the Triage task, with the thread and spec helpers.
2. `stages/spec_to_pr` and its task: approval check, implement, verify,
   bounded repair, publish, draft pull request.
3. `stages/pr_review` and its task: checkout, verify, diff-line mapping,
   COMMENT review.
4. Cancel and snapshot-refresh tasks.
5. Gateway routes for every label, command and push event.

**Exit:** on a deployed dev instance, each label produces the expected
GitHub result on the example target repository, and `/factory cancel` stops
a run and terminates its sandbox.

## Phase 6: setup and deployment

- `render.yaml`: gateway (web, Python) and workflows (workflow, Python), the
  same environment groups as the original.
- Setup scripts (1,479 lines): app manifest flow, environment groups, GitHub
  webhook and labels, preflight, doctor. Add sandbox group and credential
  creation to `setup-render`, and a brokering check to `doctor`.
- Docs: quickstart, configuration, architecture, setup runbook, extension
  runbook. The extension runbook replaces "add a harness" with "choose a
  placement".
- Deploy to Render button and fork instructions.

**Exit:** a fresh fork deploys by following the setup runbook only, and
`doctor` passes.

## Phase 7: Autopilot flow

- Autopilot as a flow over the stage functions, with steps for triage,
  implement, verify, repair, publish and review.
- Sandbox ID returned as a step result; later steps reattach.
- Run control extended to read and cancel flow runs.
- Gateway route for the autopilot label starts a flow run.
- Tests: determinism (same step sequence on replay), claim survival across
  replay, and the questions path ending the flow.

If S6 failed, this phase ports the original task-parent Autopilot instead.

**Exit:** an Autopilot run on the example target reaches a reviewed draft
pull request; killing the instance mid-run resumes without repeating a
finished stage.

## Phase 8: webinar readiness

- Re-pin and re-test against current Pydantic AI, Harness and Render SDK
  releases.
- Demo path: one agent switched between placements by changing one field,
  with the difference visible in logs (exec count, where the key lives).
- Dry run of fork-and-deploy on a clean account.
- Record known limits in the README: no cross-run dollar budget, brokered
  key can still be spent from a live sandbox, flows are new.

**Exit:** full dry run completed on the pinned versions.

## Dependencies

```text
Phase 0 ──┬──> Phase 3 ──> Phase 4 ──> Phase 5 ──> Phase 6 ──> Phase 8
Phase 1 ──┤                                │
Phase 2 ──┘ (parallel with Phase 0)        └──> Phase 7 ──> Phase 8
```

- Phase 2 needs only Phase 1.
- Phase 3 needs S1, S2 and S3.
- Phase 4 needs S4 and S5.
- Phase 7 needs S6 and Phase 5.

## Review checkpoints

- **After Phase 0:** confirm or change the Proposed decisions (D10 to D22)
  in light of the spike results. This is the cheapest point to change course.
- **After Phase 5, step 1:** review Triage end to end before building the
  other stages on the same pattern.
- **After Phase 7:** decide whether Autopilot ships as a flow or a task
  parent for the webinar.

## Out of scope

Postgres spend ledger, triage conversation carry-over, and a flows durability
backend for Pydantic AI. See spec section 11.
