# pydantic-software-factory

pydantic-software-factory is a cloud coding agent template built on Pydantic AI and Render.

Agents triage issues, write specs, implement them in isolated Render Sandboxes, and review pull requests. The project is designed to be forked and extended to your own coding workflows.

The template handles verifying GitHub webhooks, running agents against sandboxed repositories, coordinating overlapping runs and cancellation, validating agent output, publishing code safely, enforcing spend budgets, and
tracing every run in Pydantic Logfire. 

You bring the repositories, prompts, verify commands, and approval gates your
team needs. 

## How it works

1. **Label an issue.** `factory:triage` starts Triage, which reads the issue and the repository, then asks up to five questions, declines, or has Spec Writer post a spec. Reply on the issue to answer it or ask for changes.
2. **Approve the spec.** `factory:spec-approved` starts Implementer, which builds the spec in a sandbox, runs your verify commands, repairs failures up to twice, and opens a draft pull request.
3. **Review and merge.** `factory:pr-review` starts PR Reviewer, which runs your verify commands against the pull request and posts one COMMENT review. 

`factory:autopilot` runs Triage and Implementer back to back, then starts PR Reviewer on the draft pull request. 

### Architecture: Sandbox-as-tool

Each agent's loop runs in a Render Workflows task. Everything the agent *does* runs in a Render Sandbox as an
unprivileged user: shell commands go through the sandbox, and file reads, edits, and searches run there through a
small helper (`sandbox/fileops.py`), so file contents never round-trip through the host. The model key stays in
Workflows, and the sandbox has no route to the model provider.

```text
GitHub ──webhook──▶ Gateway ──task──▶ Workflows ──commands, file ops──▶ Sandbox
                       │                 │  agent loop (Pydantic AI + Harness)
                       │                 └──validated writes──▶ GitHub
                       └────▶ Postgres ◀─┘  (event log)

        both services ──traces──▶ Logfire
```

- **Gateway** (`py-factory-gateway`, a web service) verifies each webhook, records it, and starts the matching task.
- **Workflows** (`py-factory-workflows`) runs the agents and writes to GitHub.
- **Postgres** (`py-factory-db`) holds an append-only event log. It decides which run owns each issue or pull request.
- **Sandboxes** start from a per-repository snapshot with your dependencies installed, rebuilt on each push to the base branch.

See [Architecture](docs/architecture.md) for state, agents, and tracing in depth.

## How work arrives

| Trigger | Apply to | Starts |
|---|---|---|
| `factory:triage` | Issue | Triage |
| `factory:spec-approved` | Issue | Implementer, on the issue's current spec |
| `factory:pr-review` | Open pull request | PR Reviewer, on the head commit |
| `factory:autopilot` | Issue | Triage and Implementer, then PR Reviewer on the draft pull request |
| `/factory cancel` | Issue or pull request comment | Cancels the run there |

- **Labels are commands.** The factory removes a label when its run starts, so apply it again to rerun. It applies the `factory:working` label while a run is in progress.
- **Replies resume waiting runs.** After Triage asks questions or posts a spec, a comment on the issue resumes it. 
- **One run at a time.** Starting a run on an issue or pull request stops whatever was already running.

Only people with `minimum_permission` on the repository (`write` by default) can start a run, and bots can't.

## Security

- **Credentials stay out of sandboxes.** No model key, GitHub write token, Logfire token, or database credential
  enters one. Clone credentials are staged for host-controlled fetches and scrubbed before agents run.
- **Sandboxes reach an allow-list.** By default only `github.com`, plus the hosts you list for each repository.
- **The host decides every target.** Repositories, branches, and write targets come from configuration or the webhook, never from model output. The host validates every typed result again before acting on it.
- **Publishing is guarded.** It refuses symlinks, submodules, workflow files, unsafe paths, and oversized changes, and GitHub must reproduce the validated tree SHA before a commit is written.
- **Issue and pull request text is data.** Prompts tell agents to treat it as untrusted input, not instructions.


## Deploy

You need a fork of this repository, a Render workspace with Sandboxes access, a Render API key, a model API key with a provider-side spend limit, admin access to each target repository, Python 3.11+, and [uv](https://docs.astral.sh/uv/).

The [setup runbook](docs/runbooks/setup.md) walks through each step. Every command dry-runs unless you pass an
`--apply` option, and none of them deploys:

1. **Configure your fork.** Add your repositories to `factory_config.py`, then run `uv sync --locked`,
   `uv run preflight`, and `make check`.
2. **Apply the Blueprint.** [`render.yaml`](render.yaml) creates `py-factory-gateway` (Starter web service),
   `py-factory-workflows` (Workflows), `py-factory-db` (Basic-256mb Postgres), and both environment groups. Then
   give Workflows the database: `uv run setup-render --link-database --apply`.
3. **Create the GitHub App.** `uv run setup-app --gateway-url https://YOUR-GATEWAY.onrender.com` builds the manifest;
   you submit it to GitHub and exchange the one-time code.
4. **Fill in Render.** `uv run setup-render --apply-group` creates the sandbox group. You add secrets to the two
   environment groups in the Dashboard.
5. **Configure each repository.** `uv run setup-github OWNER/REPOSITORY --check`, then `--apply-labels`.
6. **Verify.** Run `uv run doctor --remote`, confirm all six Workflows tasks registered, and label a disposable issue
   with `factory:triage`. A deployment is proven by the GitHub result, not a 202 or a queued task.

Sandbox and model usage are billed separately from the services.

## Configure a repository

Each repository is an `OWNER/NAME` entry in [`factory_config.py`](factory_config.py):

| Setting | Required | What it does |
|---|---|---|
| `base_branch` | Yes | Branch used for Triage, implementation, and snapshots |
| `verify` | Yes | Commands that must pass before publishing, and that PR Reviewer runs |
| `install` | No | Dependency command, run while the snapshot is built |
| `egress` | No | Extra hosts the repository's sandboxes may reach, or `"allow-all"` |

Labels, the `/factory` command prefix, `minimum_permission`, limits, spend budgets, and trace content live in the same
file. Agent models are pinned in [`factory/agents/registry.py`](factory/agents/registry.py): Claude Sonnet for Triage
and PR Reviewer, Claude Opus for Spec Writer and Implementer. See [Configuration](docs/configuration.md) for every
setting and environment variable.

## Resources

- [Quickstart](docs/quickstart.md) and [setup runbook](docs/runbooks/setup.md)
- [Configuration](docs/configuration.md): settings, environment groups, the database, spend budgets, and Logfire
- [Architecture](docs/architecture.md): state, agents, observability, and security boundaries
- [Extension runbook](docs/runbooks/extensions.md)
- [Render Workflows](https://render.com/docs/workflows), [Render Sandboxes](https://render.com/docs/sandboxes),
  [Pydantic AI](https://ai.pydantic.dev), and [Logfire](https://logfire.pydantic.dev)

## License

MIT. See [LICENSE](LICENSE).
