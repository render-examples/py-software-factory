"""The factory's one Render Workflows application."""

from importlib import import_module

from render import Retry, Workflows

# A replay can repeat model spend and durable writes. GitHub sends a fresh trigger instead.
app = Workflows(default_retry=Retry(max_retries=0, wait_duration_ms=5_000, backoff_scaling=1))

# Import registration modules only after the shared app exists.
for _module in ("triage", "spec_to_pr", "pr_review", "cancel", "refresh_snapshot", "autopilot"):
    import_module(f"factory.workflows.{_module}")
