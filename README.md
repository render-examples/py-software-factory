# py-software-factory

A Python port of [render-examples/software-factory](https://github.com/render-examples/software-factory) (baseline
commit `fd3731d`), with Pydantic AI as the agent loop.

[implementation-plan.md](implementation-plan.md) is the single delivery plan. [spec.md](spec.md) defines the
product and security contract; `plan.md` is historical and is not an active plan.

## Status

The security-bearing core, Workflow registration, sandbox/agent runtime, Blueprint, and setup/diagnostic surface
are present. They have local automated coverage. This checkout has not been deployed or live-verified against
Render, GitHub, or credential brokering; Blueprint validation alone is not deployment proof.

## Quickstart

```bash
uv sync --locked
uv run preflight
make check
```

For deployment, follow [docs/runbooks/setup.md](docs/runbooks/setup.md). Scripts dry-run by default, secret values
stay in Render environment groups, and no setup command deploys.

## Develop

```bash
uv sync --locked
make check   # ruff, shellcheck, pyright (strict), pytest
```

Tests use fakes only: no GitHub, Render, or model calls. The publish tests run `sandbox/collect-changes.sh` against
a local git repository, so they need `git` and `bash`. `make check` requires `shellcheck`; CI installs it explicitly.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Configuration reference](docs/configuration.md)
- [Architecture and security model](docs/architecture.md)
- [Deployment/setup runbook](docs/runbooks/setup.md)
- [Agent and placement extension runbook](docs/runbooks/extensions.md)

## Verification

Local checks validate source, configuration, and fake-backed behavior only. `render blueprints validate
render.yaml` is read-only validation, not a deploy. A live deployment is verified only after gateway health,
Workflow task registration, GitHub App installation, sandbox group, and credential-brokering checks pass.

## Known limits

- There is no cross-run spend ledger or durable conversation carry-over.
- Brokering prevents key theft, not model spend: code in a live, model-enabled sandbox can use the restricted
  credential until that sandbox terminates.
- In-task tools and lifecycle polling share the workspace Sandbox API rate limit; the process-wide pacer favors
  safety over maximum concurrency.
