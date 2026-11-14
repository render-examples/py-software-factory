# py-software-factory

A Python port of [render-examples/software-factory](https://github.com/render-examples/software-factory) (baseline
commit `fd3731d`), with Pydantic AI as the agent loop. See [spec.md](spec.md) and [plan.md](plan.md).

## Status

| Phase | State |
|---|---|
| 0. Spikes | Not started: they need a dedicated Render workspace |
| 1. Scaffold | Done |
| 2. Security-bearing core | Done: `gateway`, `github`, `format`, `schemas`, `claims`, `changeset`, plus the approval check |
| 3 to 8 | Not started |

## Develop

```bash
uv sync
make check   # ruff, shellcheck, pyright (strict), pytest
```

Tests use fakes only: no GitHub, Render, or model calls. The publish tests run `sandbox/collect-changes.sh` against
a local git repository, so they need `git` and `bash`.

## Layout

- `factory_config.py`: deployment settings, validated when imported.
- `factory/gateway/`: FastAPI webhook gateway. It imports no GitHub credentials, sandboxes, agents, or claims;
  `tests/test_boundaries.py` enforces this.
- `factory/github/`: App auth, HTTP with retries, client, Git Data API, hidden markers.
- `factory/claims/`: permission check, status comment, claim lifecycle, Render run control for tasks and flows.
- `factory/changeset/`: parsing and checking the sandbox's changes file, and publishing it through the Git Data API.
- `factory/format/`: making model text safe for GitHub.
- `factory/schemas/`: agent outputs (camelCase JSON, matching the verbatim prompts) and task inputs.
- `factory/stages/`: stage functions shared by tasks and the Autopilot flow.
- `sandbox/`: shell scripts copied from the original; Phase 3 changes `install-tools.sh`, `run-agent.sh`, and
  `verify.sh`.
