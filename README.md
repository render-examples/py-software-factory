# py-software-factory

A Python port of [render-examples/software-factory](https://github.com/render-examples/software-factory) (baseline
commit `fd3731d`), with Pydantic AI as the agent loop.

## Status

The security-bearing core, Workflow registration, sandbox/agent runtime, Postgres event log, Blueprint, and
setup/diagnostic surface are present. They have local automated coverage. This checkout has not been deployed or
live-verified against Render, GitHub, or credential brokering; Blueprint validation alone is not deployment proof.

## Architecture

A FastAPI gateway (`factory-gateway`) verifies GitHub webhooks and starts tasks on a Render Workflow
(`factory-workflows`), which runs the agents with their tools in Render Sandboxes and owns every GitHub write. Both
services share a Postgres event log (`factory-db`): runs coordinate ownership of an issue or pull request there, and
the gateway records each delivery's decision there. Trigger labels are commands, removed once a run starts. A comment
on an issue resumes the run that is waiting for a reply; otherwise it starts nothing. The GitHub status comment is a
view of Postgres state. See [docs/architecture.md](docs/architecture.md).

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

Tests use fakes for GitHub, Render, and models. The publish tests run `sandbox/collect-changes.sh` against a local
git repository, so they need `git` and `bash`. `make check` requires `shellcheck`; CI installs it explicitly. The
event-log tests need a real Postgres server and are skipped without `TEST_DATABASE_URL`; see
[docs/quickstart.md](docs/quickstart.md#local-postgres).

## Documentation

- [Quickstart](docs/quickstart.md)
- [Configuration reference](docs/configuration.md)
- [Architecture and security model](docs/architecture.md)
- [Deployment/setup runbook](docs/runbooks/setup.md)
- [Agent and placement extension runbook](docs/runbooks/extensions.md)

## Verification

Local checks validate source, configuration, and fake-backed behavior only. `render blueprints validate
render.yaml` is read-only validation, not a deploy. A live deployment is verified only after gateway health,
Workflow task registration, database connection and migrations, GitHub App installation, sandbox group, and
credential-brokering checks pass.

## Known limits

- Postgres is required. Both services need `DATABASE_URL`; the Workflow gets it from
  `setup-render --link-database --apply`, because a Blueprint can't link a database to a Workflow service.
- Replies only resume waiting runs. A comment on an issue whose run isn't waiting for a reply does nothing; re-apply
  the trigger label to run again.
- The gateway's database credential can write to the event log, so a compromised gateway could forge run events
  (see [docs/architecture.md](docs/architecture.md#security-boundaries)).
- There is no cross-run spend ledger or durable conversation carry-over.
- Brokering prevents key theft, not model spend: code in a live, model-enabled sandbox can use the restricted
  credential until that sandbox terminates.
