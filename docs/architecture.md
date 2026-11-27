# Architecture and security

```text
GitHub --signed webhook--> Gateway --task input--> Render Workflow
                              |                     |  agent loop (Pydantic AI + Harness)
                              |                     |
                              |                     +--> Render Sandbox: commands and file operations
                              |                     |
                              |                     +--> validated GitHub write
                              |                     |
                              +----> Postgres <-----+
                                   (event log)

           both services ----traces----> Logfire (optional)
```

The gateway verifies the signature over raw bytes before parsing JSON, records what each delivery means, maps it to
an idempotent task start, and holds neither GitHub nor model credentials. The Workflow checks the triggering actor's
current repository permission, runs the agents, owns every GitHub write, and executes everything an agent does in a
sandbox.

## State

Postgres (`py-factory-db`, reached through asyncpg) holds an append-only event log and the spend counters. Every fact is an event on a stream such as `run:<id>`,
`thread:<key>` or `delivery:github:<id>`. Tables derived from the log (`runs`, `threads`) are updated in the same
transaction as each append, and `db-rebuild` can replay them from the log.

- **Runs and threads are coordinated in Postgres.** A run owns its issue or pull request by appending `run.started`
  to the thread's stream at the version it read. A conditional insert makes racing runs fail cleanly, so exactly one
  wins.
- **The gateway decides from Postgres, not labels.** It records each delivery's decision before starting a task, and
  a redelivery replays that decision. Labels are commands: a run removes its trigger label once it starts. An issue
  comment starts work only when the thread's latest run is waiting for a reply.
- **The status comment is a view.** It and the `factory:working` label are synced from Postgres on a best-effort
  basis. Nothing reads them back to make decisions, so deleting the comment or a GitHub outage doesn't break
  coordination.

GitHub owns issues, labels, specifications, draft pull requests and reviews. Render Workflows runs the tasks, and
sandbox snapshots hold prepared repositories.

## Agents

Each agent is a Pydantic AI agent with a typed `submit` output, a prompt from `factory/agents/prompts/`, a turn limit,
and one of two tool sets (`factory/agents/tools.py`):

- **Read-only** (Triage, Spec Writer, PR Reviewer): `read_file`, `list_files`, `grep`, and a Harness `Shell`
  limited to read-only commands such as `git`, `rg` and `cat`, with no output redirection.
- **Writing** (Implementer): the same plus `edit_file` and `write_file`, and an unrestricted `Shell`.

All agents also carry Pydantic AI Harness capabilities for long runs: `ClearToolResults` keeps old tool output from
filling the context window, `DetectLoops` notices repeated calls that make no progress, `RepairToolArguments`
fixes malformed tool-call JSON, and `SpendLimits` stops an agent once a run, repository, or deployment budget is
spent. Spend is counted in `py-factory-db`, so every Workflow instance enforces the same budgets (see
[Spend budgets](configuration.md#spend-budgets)).

The agent loop runs in the Workflow task, so the model key never leaves Workflow compute and the sandbox has no route
to the model provider. Everything the agent does runs in the sandbox as the unprivileged `factory` user:

- **Commands** go through `SandboxWorkspace`, a commands-only Pydantic AI workspace.
- **File operations** are sent to `sandbox/fileops.py`, a stdlib helper installed in every snapshot. One request
  reads a window of a file, applies a batch of exact replacements, writes a file, lists files, or greps, and returns
  only what the model asked for. File contents never round-trip through the host: an edit sends the old and new text,
  not the file. Paths are resolved in the sandbox, symlinks included, and must stay inside the repository; `.git` is
  never writable. A request too large for one command argument is uploaded to a staging file instead.

Snapshots record a hash of the factory scripts they were built with. A sandbox restored from a snapshot with other
scripts is discarded and the snapshot rebuilt once, so a deployment that changes `fileops.py` or any other sandbox
script takes effect on the next run without a manual refresh.

## Observability

Both services send OpenTelemetry traces to [Pydantic Logfire](https://logfire.pydantic.dev) when `LOGFIRE_TOKEN`
is set (`factory/tracing.py`). One trace follows a delivery from the gateway through every task it starts: task
inputs carry a `trace_context`, Autopilot passes its trace to its children and to PR Reviewer, and each task's run
span continues it. Spans carry the Render task run IDs (`factory.trace_id`, `factory.run_id`) as baggage, so every
Pydantic AI and HTTP span under a run can be found from the Render Dashboard.

The trace shows the gateway's decision; each run's progress and outcome; sandbox restores; every agent run, model
request (with tokens and cost), and tool call; the Sandbox API, GitHub, and Anthropic requests underneath them; each
verify command; and publishing. Traces are a view like the status comment: nothing reads them back to decide anything.

## Security boundaries

- Repositories, refs, branches, and write targets come from trusted configuration or webhook input, never model
  output.
- GitHub installation tokens are staged only for host-controlled fetches and scrubbed before agent execution. No
  model key, GitHub token, or Logfire token is ever in a sandbox.
- The host validates typed model output again before publishing.
- GitHub reproduces the validated tree SHA before a write is accepted.
- Automation stops at draft pull requests and COMMENT reviews.
- Every sandbox has trusted egress policy, a provider timeout, and `finally` cleanup.
- The read-only command allowlist and the file helper's path checks are defense in depth. The sandbox and
  host-owned writes are the security boundaries: nothing a read-only agent changes in its sandbox is published.
- **Telemetry content.** With `telemetry.include_content=False` (the default), traces carry timing, models, tokens,
  cost, tool names and outcomes, but no prompts, model output, or tool arguments and results. Turning it on sends
  issue text and repository contents to Logfire. HTTP headers and bodies are never captured, and every exported
  string has known credential shapes masked.
- The gateway holds `DATABASE_URL`, which can write to the event log. A compromised gateway could forge run events,
  such as marking a thread as waiting or appending `run.started` to displace a run, and could misgroup traces by
  passing a false trace context. It still can't write to GitHub, read repositories, or reach a model. This is
  accepted so the gateway can decide from recorded state. An optional hardening step is a separate Postgres role for
  the gateway that can only insert and read events and read `threads` and `runs`; because appends update the derived
  tables in the same transaction, that needs a `SECURITY DEFINER` append function or trigger-based projections.
- `py-factory-db` accepts connections only from inside the workspace (`ipAllowList: []`). Operators add their IP
  temporarily to run diagnostics from a laptop. Setup and diagnostic commands never print the database URL.
