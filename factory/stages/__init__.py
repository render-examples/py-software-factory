"""Plain stage functions shared by the single-stage tasks and the Autopilot flow."""

from factory.stages.approval import ApprovedSpec, load_approved_spec

__all__ = ["ApprovedSpec", "load_approved_spec"]
