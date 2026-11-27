"""The factory's one Render Workflows application."""

from importlib import import_module

from render import Retry, Workflows

from factory import tracing
from factory.config import get_config

tracing.configure("py-factory-workflows")
tracing.instrument_workflows(include_content=get_config().telemetry.include_content)

# A replay can repeat model spend and durable writes. GitHub sends a fresh trigger instead.
app = Workflows(default_retry=Retry(max_retries=0, wait_duration_ms=5_000, backoff_scaling=1))

# Import registration modules only after the shared app exists.
for _module in ("triage", "spec_to_pr", "pr_review", "cancel", "refresh_snapshot", "autopilot"):
    import_module(f"factory.workflows.{_module}")
