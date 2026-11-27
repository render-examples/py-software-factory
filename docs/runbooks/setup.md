# Deployment and setup runbook

This runbook separates source validation, account mutation, deployment, and live verification. Commands are
dry-run/read-only unless marked **mutating**. Never paste secrets into command history.

## 1. Prepare the fork

1. Configure `factory_config.py` for each `OWNER/REPOSITORY`.
2. Run `uv sync --locked`, `uv run preflight`, and `make check`.
3. Run `render blueprints validate render.yaml`. This does not deploy.

## 2. Create the sandbox group and environment groups

The Blueprint links the services to two environment groups, `py-factory-gateway` and `py-factory-workflows`, but
doesn't create them, so they must exist first. Start with the sandbox group, dry run first, then **mutating**:

```bash
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --apply-group
```

Put the resulting `sbg-...` ID in `.env` as `FACTORY_SANDBOX_GROUP_ID`, along with `RENDER_API_KEY`,
`RENDER_WORKSPACE_ID`, and `ANTHROPIC_API_KEY` (see [configuration.md](../configuration.md)). Then create both groups
from `.env`, dry run first, then **mutating**:

```bash
uv run setup-render --env-groups
uv run setup-render --env-groups --apply
```

It reads Render credentials from `.env` or the environment, creates each missing group, and sets each of that
service's keys that has a value, overwriting the group's current value. It never deletes a key, never prints a value,
and never copies `LOGFIRE_TOKEN`, `DATABASE_URL`, or `RENDER_ADMIN_API_KEY`. Never add `RENDER_ADMIN_API_KEY` to a
service.

For tracing, create two Logfire write tokens in your Logfire project, one per service, and add them as
`LOGFIRE_TOKEN` to the `py-factory-gateway` and `py-factory-workflows` groups in the Dashboard. Create them in the
project's settings, or with `uvx logfire-cli token write` from a directory where `uvx logfire-cli init` has selected
the project; it prints the new token once, so paste it straight into the Dashboard. Set `LOGFIRE_PROJECT_URL` in
`.env` before running `--env-groups` so status comments link each run's trace. Decide whether traces include prompts
and repository content before the first run (see [Observability](../configuration.md#observability-logfire)).

## 3. Create the Render services

Create a Blueprint from the fork's `render.yaml`. Review the proposed resources before applying:

- web service `py-factory-gateway`, Python, `starter`, with `DATABASE_URL` linked from `py-factory-db`
- Workflow `py-factory-workflows`, Python, with no service-level plan
- Postgres 17 database `py-factory-db`, `basic-256mb`, with no external access

The Workflow inherits the repository containing the Blueprint. If Render validation asks for an explicit `repo`
URL, use the fork URL in the Dashboard rather than committing a canonical repository URL that would break forks.

A Blueprint apply/deploy is an external mutation and is intentionally not performed by any setup script. The gateway
starts without `GITHUB_WEBHOOK_SECRET` and answers webhooks with 503 until section 4 adds it; wait for its URL before
creating the GitHub App.

Then give the Workflow the database URL. A Blueprint can't link a database to a Workflow service, so copy the
gateway's value into the `py-factory-workflows` group. Dry run first, then **mutating**:

```bash
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --link-database
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --link-database --apply
```

Redeploy `py-factory-workflows` so it picks up the value. The command never prints it. See
[Database](../configuration.md#database).

## 4. Register the GitHub App

Generate a manifest and local POST form:

```bash
uv run setup-app \
  --gateway-url https://YOUR-GATEWAY.onrender.com \
  --output tmp/github-app-manifest.json \
  --html-output tmp/create-github-app.html
```

For organization ownership, add `--organization ORGANIZATION`. Inspect the JSON, open the HTML locally, and submit
it to GitHub. The redirect lands on the gateway root with a one-time `code` in the address bar. Exchange it within
one hour:

```bash
uv run setup-app --exchange-code CODE --credentials-output tmp/github-app.env
```

This exchange is **mutating** and consumes the one-time code. The output file is mode 0600 and contains the App ID,
private key, and webhook secret. Copy them into the groups, dry run first, then **mutating**:

```bash
uv run setup-render --env-groups --env-file tmp/github-app.env
uv run setup-render --env-groups --env-file tmp/github-app.env --apply
```

Then securely delete the file and redeploy both services so they see the new values.

Install the App on only the configured repositories. The manifest subscribes to `issues`, `issue_comment`,
`pull_request`, and `push`, with contents/write, issues/write, members/read, metadata/read, and pull-requests/write.

## 5. Configure repositories

First validate App access without writes:

```bash
uv run setup-github OWNER/REPOSITORY --check
```

Then explicitly create/update labels (**mutating**):

```bash
uv run setup-github OWNER/REPOSITORY --apply-labels
```

Repeat for every repository in `factory_config.py`.

## 6. Deploy and verify

Deploy from a committed, pushed revision. Environment-group changes need a deploy before the running service sees
them. Then run:

```bash
uv run doctor --remote \
  --gateway-url https://YOUR-GATEWAY.onrender.com \
  --repository OWNER/REPOSITORY \
  --env-file .env
```

Also verify in Render that `py-factory-workflows` registered all six tasks: `triage`, `spec-to-pr`, `pr-review`,
`autopilot`, `cancel`, and `refresh-snapshot`.

`doctor --remote` checks that `py-factory-workflows` holds `DATABASE_URL` and reports which groups hold `LOGFIRE_TOKEN`
(by name only), and that the database connects and is migrated. For the connection, put `py-factory-db`'s external URL in `.env` as `DATABASE_URL` and allow your IP on
`py-factory-db` while it runs (see [Database](../configuration.md#database)).

Finally send a GitHub `ping`, inspect Recent Deliveries, and trigger Triage on a disposable issue. Success requires
the expected GitHub result, not merely HTTP 202 or a queued task. The run should also appear as rows in the `runs`
and `threads` tables and, with tracing on, as one Logfire trace from the gateway delivery through the agent run.

The first run after a deploy that changes any `sandbox/` script rebuilds the repository snapshot inline, so it takes
several minutes longer.
