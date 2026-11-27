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
`make check` runs ruff, shellcheck, pyright (strict), and pytest.

The test suite fakes GitHub, Render, and the model, so it spends no model tokens. It still runs the real agent tool
loop, `sandbox/fileops.py`, and shell commands against a local checkout, and the publish tests run
`sandbox/collect-changes.sh` against a local git repository, so it needs `git` and `bash`; search tests also use `rg`
when it's installed.

## Local Postgres

The event-log tests run against a real Postgres server (13 or newer). Without one they are skipped, and
`make check` still passes. With Homebrew:

```bash
brew install postgresql@16
brew services start postgresql@16
TEST_DATABASE_URL=postgresql:///postgres uv run pytest
```

`TEST_DATABASE_URL` must name a server where the tests may create databases: each test module creates its own
`sf_test_<uuid>` database and drops it afterwards. Postgres tests run one at a time; don't add `pytest-xdist`.

To run the gateway or Workflow tasks locally, create a database and point both at it. Each service applies pending
migrations the first time it connects:

```bash
createdb factory
echo 'DATABASE_URL=postgresql:///factory' >> .env
uv run preflight   # connects and reports pending migrations
```

To use the deployed `py-factory-db` instead, see [Database](configuration.md#database).

## Setup commands

All commands are installed by `uv sync`:

```bash
uv run setup-app --gateway-url https://YOUR-GATEWAY.onrender.com
uv run setup-render
uv run setup-render --env-groups
uv run setup-render --link-database
uv run setup-github OWNER/REPOSITORY
uv run doctor
```

These are dry-run or read-only unless an explicit `--apply*` option is used. They never deploy. See the
[setup runbook](runbooks/setup.md) before using an apply option. `uv run db-rebuild` is not a dry run: it rewrites
the derived tables from the event log (see [Database](configuration.md#database)).

## Deployment readiness

Validate the Blueprint without applying it:

```bash
render blueprints validate render.yaml
```

The Blueprint defines exactly two Python services, `py-factory-gateway` (web) and `py-factory-workflows` (Workflow), and one
Postgres database, `py-factory-db`, linked to the gateway. There is no service-level plan on the Workflow. The build uses
the checked-in uv lock. It links each service to an environment group without creating it, so run
`setup-render --env-groups --apply` before applying it, and `setup-render --link-database --apply` afterwards so the
Workflow gets `DATABASE_URL` too.

Blueprint validation does not start the Workflow or prove task registration. Do not describe it as a successful
deployment; complete the live checks in the setup runbook.
