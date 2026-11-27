# pydantic-software-factory

A GitHub-native software factory on Render: Pydantic AI agents with Pydantic AI Harness capabilities, Render
Workflows and Sandboxes, and Pydantic Logfire tracing. A Python port of
[render-examples/software-factory](https://github.com/render-examples/software-factory) (baseline commit `fd3731d`).

## Status

The security-bearing core, Workflow registration, sandbox/agent runtime, Postgres event log, Logfire tracing,
Blueprint, and setup/diagnostic surface are present. They have local automated coverage. This checkout has not been
deployed or live-verified against Render, GitHub, or Logfire; Blueprint validation alone is not deployment proof.

## Architecture

A FastAPI gateway (`py-factory-gateway`) verifies GitHub webhooks and starts tasks on a Render Workflow
(`py-factory-workflows`). The Workflow runs each agent's loop and owns every GitHub write; everything an agent does,
every command and file operation, runs in a Render Sandbox as an unprivileged user, and file operations run where the
files are instead of moving them to the host. Both services share a Postgres event log (`py-factory-db`): runs
coordinate ownership of an issue or pull request there, and the gateway records each delivery's decision there.
Trigger labels are commands, removed once a run starts. A comment on an issue resumes the run that is waiting for a
reply; otherwise it starts nothing. The GitHub status comment is a view of Postgres state, and Logfire traces each
delivery through every task, agent run, model request, and tool call it causes. See
[docs/architecture.md](docs/architecture.md).

## Quickstart

```bash
uv sync --locked
uv run preflight
make check
```

For deployment, follow [docs/runbooks/setup.md](docs/runbooks/setup.md). Scripts dry-run by default, secret values
stay in Render environment groups, and no setup command deploys. Existing deployments must follow the
[upgrade order](docs/runbooks/setup.md#upgrading-an-existing-deployment): the database lands before the code that
uses it.

## Develop

```bash
uv sync --locked
make check   # ruff, shellcheck, pyright (strict), pytest
```

Tests use fakes for GitHub and Render and scripted Pydantic AI models. The agent tests run the real tool loop,
`sandbox/fileops.py`, and shell commands against a local checkout, and the publish tests run
`sandbox/collect-changes.sh` against a local git repository, so they need `git` and `bash`; search tests also use
`rg` when it is installed. `make check` requires `shellcheck`; CI installs it explicitly. The event-log tests need a
real Postgres server and are skipped without `TEST_DATABASE_URL`; see
[docs/quickstart.md](docs/quickstart.md#local-postgres).

## Documentation

- [Quickstart](docs/quickstart.md)
- [Configuration reference](docs/configuration.md)
- [Architecture and security model](docs/architecture.md)
- [Deployment/setup runbook](docs/runbooks/setup.md)
- [Extension runbook](docs/runbooks/extensions.md)

## Verification

Local checks validate source, configuration, and fake-backed behavior only. `render blueprints validate
render.yaml` is read-only validation, not a deploy. A live deployment is verified only after gateway health,
Workflow task registration, database connection and migrations, GitHub App installation, and sandbox group checks
pass, and, with tracing on, a delivery appears in Logfire as one trace.

## Known limits

- Postgres is required. Both services need `DATABASE_URL`; the Workflow gets it from
  `setup-render --link-database --apply`, because a Blueprint can't link a database to a Workflow service.
- Replies only resume waiting runs. A comment on an issue whose run isn't waiting for a reply does nothing; re-apply
  the trigger label to run again.
- The gateway's database credential can write to the event log, so a compromised gateway could forge run events
  (see [docs/architecture.md](docs/architecture.md#security-boundaries)).
- Spend budgets stop the next model request once a budget is spent, so concurrent runs can overshoot by about one
  request each; keep a provider-side limit as well. There is no durable conversation carry-over.
- The first run after a deploy that changes a `sandbox/` script rebuilds that repository's snapshot inline.
