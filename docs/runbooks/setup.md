# Deployment and setup runbook

This runbook separates source validation, account mutation, deployment, and live verification. Commands are
dry-run/read-only unless marked **mutating**. Never paste secrets into command history.

## 1. Prepare the fork

1. Configure `factory_config.py` for each `OWNER/REPOSITORY`.
2. Run `uv sync --locked`, `uv run preflight`, and `make check`.
3. Run `render blueprints validate render.yaml`. This does not deploy.
4. Confirm `factory/workflows/app.py` exists and registers the expected tasks before deploying. If it is absent,
   the current implementation milestone is not deployable.

## 2. Create the Render services

Create a Blueprint from the fork's `render.yaml`. Review the proposed resources before applying:

- web service `factory-gateway`, Python, `starter`, with `DATABASE_URL` linked from `factory-db`
- Workflow `factory-workflows`, Python, with no service-level plan
- Postgres 17 database `factory-db`, `basic-256mb`, with no external access
- environment groups `factory-gateway` and `factory-workflows`

The Workflow inherits the repository containing the Blueprint. If Render validation asks for an explicit `repo`
URL, use the fork URL in the Dashboard rather than committing a canonical repository URL that would break forks.

Wait for the gateway URL before creating the GitHub App. A Blueprint apply/deploy is an external mutation and is
intentionally not performed by any setup script.

Then give the Workflow the database URL. A Blueprint can't link a database to a Workflow service, so copy the
gateway's value into the `factory-workflows` group. Dry run first, then **mutating**:

```bash
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --link-database
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --link-database --apply
```

Redeploy `factory-workflows` so it picks up the value. The command never prints it. See
[Database](../configuration.md#database).

## 3. Register the GitHub App

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
private key, and webhook secret. Transfer values to Render, then securely delete the file.

Install the App on only the configured repositories. The manifest subscribes to `issues`, `issue_comment`,
`pull_request`, and `push`, with contents/write, issues/write, members/read, metadata/read, and pull-requests/write.

## 4. Configure Render

Run the read-only/dry-run handoff:

```bash
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render
```

Create the sandbox group explicitly:

```bash
RENDER_API_KEY=... RENDER_WORKSPACE_ID=... uv run setup-render --apply-group
```

Put the resulting `sbg-...` ID in `FACTORY_SANDBOX_GROUP_ID` on `factory-workflows`. Populate both environment
groups exactly as described in [configuration.md](../configuration.md). Never add `RENDER_ADMIN_API_KEY` to a
service.

For tracing, create two Logfire write tokens in your Logfire project, one per service, and add them as
`LOGFIRE_TOKEN` to the `factory-gateway` and `factory-workflows` groups. Decide whether traces include prompts and
repository content before the first run (see [Observability](../configuration.md#observability-logfire)).

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

Also verify in Render that all expected Workflow tasks registered.

`doctor --remote` checks that `factory-workflows` holds `DATABASE_URL` and reports which groups hold `LOGFIRE_TOKEN`
(by name only), and that the database connects and is migrated. For the connection, put `factory-db`'s external URL in `.env` as `DATABASE_URL` and allow your IP on
`factory-db` while it runs (see [Database](../configuration.md#database)).

Finally send a GitHub `ping`, inspect Recent Deliveries, and trigger Triage on a disposable issue. Success requires
the expected GitHub result, not merely HTTP 202 or a queued task. The run should also appear as rows in the `runs`
and `threads` tables and, with tracing on, as one Logfire trace from the gateway delivery through the agent run.

The first run after a deploy that changes any `sandbox/` script rebuilds the repository snapshot inline, so it takes
several minutes longer.

## Upgrading an existing deployment

A deployment from before the event log has no database, and its services deploy on `checksPass`, so merging code
deploys both. Land the database plumbing first, in this order:

1. **Blueprint first.** Merge only the `render.yaml` changes and sync the Blueprint. This creates `factory-db` and
   gives the gateway `DATABASE_URL`. The current gateway code ignores it.
2. **Link Workflows.** Run `setup-render --link-database --apply`, redeploy `factory-workflows`, and confirm with
   `doctor --remote`.
3. **Drain.** Wait until nothing is running, or cancel runs from the Render Dashboard. Runs started under the old
   status-comment claim aren't in Postgres, so a new run can't stop them.
4. **Merge the event log and Postgres runs.** Workflows migrates on its first task. Coordination moves to Postgres;
   the gateway still routes by label.
5. **Merge gateway decisions.** The gateway migrates at startup. Labels become commands, and replies resume only
   waiting runs.
6. **Re-trigger waiting issues.** Issues that asked questions before the upgrade have no waiting run. Re-apply
   `factory:triage` or `factory:autopilot` on any that still need answers, and tell maintainers.

Rolling back gateway decisions restores label routing safely. Rolling back Postgres runs after runs have been
recorded loses nothing, but brings back the status-comment claim's races.
