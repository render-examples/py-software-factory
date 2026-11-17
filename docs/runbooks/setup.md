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

- web service `factory-gateway`, Python, `starter`
- Workflow `factory-workflows`, Python, with no service-level plan
- environment groups `factory-gateway` and `factory-workflows`
- no database

The Workflow inherits the repository containing the Blueprint. If Render validation asks for an explicit `repo`
URL, use the fork URL in the Dashboard rather than committing a canonical repository URL that would break forks.

Wait for the gateway URL before creating the GitHub App. A Blueprint apply/deploy is an external mutation and is
intentionally not performed by any setup script.

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

Credential brokering is optional. The default `brokered_model_access=False` keeps every model loop in Workflow
compute while tools remain sandboxed. To enable in-sandbox loops, configure the restricted credential with:

```bash
RENDER_ADMIN_API_KEY=... ANTHROPIC_API_KEY=... \
  uv run setup-render --group-id sbg-... --apply-credential
```

Run the live broker probe, then set `brokered_model_access=True` and redeploy. If credential administration is
unavailable, keep the safe default; never copy `ANTHROPIC_API_KEY` into a sandbox.

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

Also verify in Render that all expected Workflow tasks registered. To perform the broker probe in a disposable
sandbox, rerun the command with `--probe-brokering`. This creates a sandbox and makes one minimal billable model
request, so use it only in an authorized workspace. The implementation sends no provider key in input, files,
commands, or environment.

Finally send a GitHub `ping`, inspect Recent Deliveries, and trigger Triage on a disposable issue. Success requires
the expected GitHub result, not merely HTTP 202 or a queued task.
