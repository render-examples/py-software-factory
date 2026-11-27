# Extending the factory

## Add or change an agent

1. Define a stable agent ID, prompt, typed output, turn limit (`max_turns`), model, and whether it `writes` in
   `factory/agents/registry.py`. Read-only agents get the read tools and a read-only shell; only writing agents get
   `edit_file` and `write_file`.
2. Keep every model-directed tool acting on the sandbox: commands through `SandboxWorkspace`, files through
   `sandbox/fileops.py`. Host GitHub clients are never agent tools.
3. Add Pydantic AI Harness capabilities in `factory/agents/tools.py`. A capability that reads or writes files through
   the workspace (such as `FileSystem`, `ToolOutputLimits` spills, or `Memory` on a workspace store) needs a
   filesystem-capable backend, which `SandboxWorkspace` deliberately isn't; add a `fileops.py` operation instead.
4. Add scripted-model tests in `tests/test_agents.py` for valid output, refused tool calls, turn limits, and the
   exact tools the agent is offered. The `LocalSandbox` there runs the real helper and shell locally.
5. Call the agent from a stage in `factory/stages/`. For a new task, define it with `define_github_task` in a module
   under `factory/workflows/`, add its name to `Tasks` in `factory/config.py`, and add the module to the import list
   in `factory/workflows/app.py`. Keep task input and output JSON-serializable.
6. Update configuration and docs without introducing a second plan source.

## Route a new event, label, or command

`route()` in `factory/gateway/routes.py` turns each verified delivery into a decision: start a task, ignore the
delivery with a reason, or look up the thread first (how a reply finds the run waiting for it). Add a branch there,
and validate the task input with its `TaskInput` model so a bad payload is recorded as invalid instead of starting
work. Cover it in `tests/test_routes.py`.

- **A label.** Add it to `Labels` in `factory/config_model.py` and to `desired_labels()` in `scripts/setup_github.py`,
  then rerun `setup-github OWNER/NAME --apply-labels`.
- **A comment command.** Match the comment's first line against `config.command_prefix`, as `/factory cancel` does.
- **A GitHub event.** Add it to `EVENTS` in `scripts/setup_app.py`, and subscribe an existing App to it in the App's
  settings on GitHub.

The gateway records the decision before starting anything and has no GitHub or model credential, so keep routing to
reading the payload and Postgres.

## Add a file operation

Add a function to `OPERATIONS` in `sandbox/fileops.py` and a tool in `factory/agents/tools.py` that calls it with
`ctx.deps.files.request(...)`. Keep it to the standard library, resolve every path with `resolve()`, return only what
the model needs, and stay under the 256 KB exec output tail. Changing `fileops.py` changes the snapshot scripts hash,
so the next run rebuilds each repository's snapshot inline.

## Add a model provider

Add the provider's Pydantic AI extra to `pydantic-ai-slim` in `pyproject.toml`, use its model string in the agent
definition, and add its key variable to the `py-factory-workflows` group. Agent loops run in the Workflow task, so the
sandbox needs no egress to the provider.

## Add a span

Record factory work Pydantic AI can't see with `logfire.span(...)` in the module that does it, using a message
template and attributes rather than formatted strings, so Logfire can scrub and query them. Runs already carry the
factory IDs as baggage. Never attach credentials, request headers, or file contents to a span.

## Change the event log

Define new event types with `define_event` in `factory/events/catalog.py`, next to the existing ones. Schema changes are new
numbered files in `factory/db/migrations/`; never edit a migration that has been deployed. The services apply
pending migrations themselves, and `doctor` reports any that are pending. If a projection change alters derived
rows, run `uv run db-rebuild` after deploying it (see [Database](../configuration.md#database)).

## Add a repository

Add an exact `OWNER/NAME` key in `factory_config.py`, including base branch, install/verify commands, and the
smallest egress allow-list. Install the GitHub App on that repository and run `setup-github OWNER/NAME --check`
before applying labels.
