# Architecture and security

```text
GitHub --signed webhook--> Gateway --task input--> Render Workflow
                                                    |
                                                    +--> Render Sandbox
                                                    |
                                                    +--> validated GitHub write
```

The gateway verifies the signature over raw bytes before parsing JSON, maps each delivery to an idempotent task
start, and holds neither GitHub nor model credentials. The Workflow checks the triggering actor's current
repository permission, owns every GitHub write, and always runs model-directed tools inside a sandbox.

GitHub owns issues, labels, specifications, draft pull requests, reviews, and the status-comment claim. Render
Workflows owns run state. Sandbox snapshots hold prepared repositories. There is no database.

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

See [spec.md](../spec.md) for the complete invariant list.
