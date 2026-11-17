# Quickstart

## Local validation

Install Python 3.11+, `uv`, `git`, `bash`, and `shellcheck`, then run:

```bash
uv sync --locked
cp .env.example .env
uv run preflight
make check
```

`preflight` does not call Render or GitHub. Missing secrets are warnings unless `--require-secrets` is supplied.
The test suite uses fakes and does not spend model tokens.

## Setup commands

All commands are installed by `uv sync`:

```bash
uv run setup-app --gateway-url https://YOUR-GATEWAY.onrender.com
uv run setup-render
uv run setup-github OWNER/REPOSITORY
uv run doctor
```

These are dry-run or read-only unless an explicit `--apply-*` option is used. They never deploy. See the
[setup runbook](runbooks/setup.md) before using an apply option.

## Deployment readiness

Validate the Blueprint without applying it:

```bash
render blueprints validate render.yaml
```

The Blueprint defines exactly two Python services: `factory-gateway` (web) and `factory-workflows` (Workflow).
There is no database and no service-level plan on the Workflow. The build uses the checked-in uv lock.

Blueprint validation does not start the Workflow or prove task registration. Do not describe it as a successful
deployment; complete the live checks in the setup runbook.
