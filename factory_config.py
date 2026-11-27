"""The factory's deployment settings. See docs/configuration.md."""

from factory.config_model import FactoryConfig, Labels, Limits, RepoConfig, SandboxSettings, TelemetrySettings

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
    telemetry=TelemetrySettings(include_content=False),
)
