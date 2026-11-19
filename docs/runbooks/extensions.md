# Extending the factory

## Add or change an agent

1. Define a stable agent ID, prompt, typed output, tool allow-list, request limit, timeout, model, and placement.
2. Keep all model-directed tools on the sandbox workspace. Host GitHub clients are never agent tools.
3. Add scripted model tests for valid output, rejected output, request limits, timeout mapping, and exact tool
   authority.
4. Add the stage/task mapping and preserve JSON-serializable task input and output.
5. Update configuration and docs without introducing a second plan source.

## Choose a placement

Prefer **in-task** for read-only agents with frequent small tool calls when brokered model access is unnecessary.
The provider key stays in Workflow compute; omit the provider hostname from sandbox egress.

Prefer **in-sandbox** for write-capable or long-running agents where local tool calls reduce Sandbox API traffic.
This placement requires the v3 wire protocol, runner snapshot dependencies, provider hostname in trusted egress,
and a functioning sandbox-group broker credential.

To switch placement:

1. Change the agent definition, not stage code.
2. Recalculate egress: model host allowed only for in-sandbox execution.
3. Verify the in-task path uses the attached sandbox workspace, or the in-sandbox path uses the detached runner and
   ends with exactly one typed result/error.
4. Assert the provider key is absent from sandbox input, uploads, commands, files, and environment.
5. Run the credential broker probe for in-sandbox placement.
6. Recheck request limits, timeout, cleanup, cancellation, and telemetry.

Do not switch to in-sandbox by injecting `ANTHROPIC_API_KEY`. If brokering is unavailable, keep the agent in-task
or stop the rollout.

## Add a model provider

Add its model string, key variable, exact API hostname, and broker method/path restriction to the provider table.
Update the Workflow environment-group documentation for in-task use and the sandbox-group credential setup for
in-sandbox use. Never use wildcard model hosts when an exact host is available.

## Change the event log

Define new event types with `define_event` in the part of the code that first records them. Schema changes are new
numbered files in `factory/db/migrations/`; never edit a migration that has been deployed. The services apply
pending migrations themselves, and `doctor` reports any that are pending. If a projection change alters derived
rows, run `uv run db-rebuild` after deploying it (see [Database](../configuration.md#database)).

## Add a repository

Add an exact `OWNER/NAME` key in `factory_config.py`, including base branch, install/verify commands, and the
smallest egress allow-list. Install the GitHub App on that repository and run `setup-github OWNER/NAME --check`
before applying labels.
