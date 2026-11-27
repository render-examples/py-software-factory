from decimal import Decimal

from factory.config_model import (
    FactoryConfig,
    Labels,
    Limits,
    RepoConfig,
    SandboxSettings,
    SpendSettings,
    TelemetrySettings,
)

CONFIG = FactoryConfig(
    repos={
        "jacobprall/streamlit-example": RepoConfig(
            base_branch="master",
            install=(
                "uv venv /opt/factory/scratch/home/project-venv && "
                "uv pip install --python /opt/factory/scratch/home/project-venv/bin/python -r requirements.txt"
            ),
            verify=("/opt/factory/scratch/home/project-venv/bin/python -m py_compile streamlit_app.py",),
            egress=("pypi.org", "files.pythonhosted.org"),
        ),
    },
    labels=Labels(
        triage="factory:triage",
        spec_approved="factory:spec-approved",
        pr_review="factory:pr-review",
        autopilot="factory:autopilot",
    ),
    command_prefix="/factory",
    minimum_permission="write",
    limits=Limits(
        repair_rounds=2,
        max_changed_files=200,
        max_file_bytes=1_000_000,
    ),
    sandbox=SandboxSettings(timeout_seconds=4 * 60 * 60),
    spend=SpendSettings(agent_run_usd=Decimal(20), repo_day_usd=Decimal(100), day_usd=Decimal(250)),
    telemetry=TelemetrySettings(include_content=False),
)
