# Architecture and security

```text
GitHub --signed webhook--> Gateway --task input--> Render Workflow
                              |                     |
                              |                     +--> Render Sandbox
                              |                     |
                              |                     +--> validated GitHub write
                              |                     |
                              +----> Postgres <-----+
                                   (event log)
```

The gateway verifies the signature over raw bytes before parsing JSON, records what each delivery means, maps it to
an idempotent task start, and holds neither GitHub nor model credentials. The Workflow checks the triggering actor's
current repository permission, owns every GitHub write, and always runs model-directed tools inside a sandbox.

## State

Postgres (`factory-db`) holds an append-only event log. Every fact is an event on a stream such as `run:<id>`,
`thread:<key>` or `delivery:github:<id>`. Tables derived from the log (`runs`, `threads`, `agent_runs`) are updated
in the same transaction as each append, and `db-rebuild` can replay them from the log.

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

## Agent placement

Both placements use the same typed agent definition and sandboxed tools:

- **Agent in task:** the Pydantic AI loop runs in Workflow compute; each tool call executes through the attached
  sandbox. The provider key is available only to the Workflow process, and model egress is omitted from the
  sandbox.
- **Agent in sandbox:** the host uploads the runner and typed input, then the full loop executes in the sandbox.
  Model access uses the sandbox-group credential broker. The provider key is never in runner input, files, command
  lines, or environment.

Agent definitions declare Triage and Spec Writer in-task, with Implementer and PR Reviewer in-sandbox. The
deployment uses those in-sandbox placements only when `sandbox.brokered_model_access` is enabled. Otherwise all
four loops run in Workflow compute while retaining exactly the same sandboxed tools. Moving an agent is an
authority decision, not just a performance toggle: placement, provider egress, broker availability, and tool
authority are tested together.

## Security boundaries

- Repositories, refs, branches, and write targets come from trusted configuration or webhook input, never model
  output.
- GitHub installation tokens are staged only for host-controlled fetches and scrubbed before agent execution.
- The host validates typed model output again before publishing.
- GitHub reproduces the validated tree SHA before a write is accepted.
- Automation stops at draft pull requests and COMMENT reviews.
- Every sandbox has trusted egress policy, a provider timeout, and `finally` cleanup.
- Shell policy is defense in depth; sandbox isolation and host-owned writes are the security boundaries.
- Brokering prevents model-key theft, not model spend. Method/path restrictions, request limits, and timeouts
  remain required.
- The gateway holds `DATABASE_URL`, which can write to the event log. A compromised gateway could forge run events,
  such as marking a thread as waiting or appending `run.started` to displace a run. It still can't write to GitHub,
  read repositories, or reach a model. This is accepted so the gateway can decide from recorded state. An optional
  hardening step is a separate Postgres role for the gateway that can only insert and read events and read `threads`
  and `runs`; because appends update the derived tables in the same transaction, that needs a `SECURITY DEFINER`
  append function or trigger-based projections.
- `factory-db` accepts connections only from inside the workspace (`ipAllowList: []`). Operators add their IP
  temporarily to run diagnostics from a laptop. Setup and diagnostic commands never print the database URL.
