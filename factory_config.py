"""The factory's deployment settings. See docs/configuration.md."""

from factory.config_model import FactoryConfig, Labels, Limits, RepoConfig, SandboxSettings

CONFIG = FactoryConfig(
    repos={
        "render-examples/render-factory-example-target": RepoConfig(
            base_branch="main",
            install="npm ci",
            verify=("npm run build", "npm test"),
            egress=("registry.npmjs.org",),
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
    sandbox=SandboxSettings(
        timeout_seconds=4 * 60 * 60,
    ),
)
