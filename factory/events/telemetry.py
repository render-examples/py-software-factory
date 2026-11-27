"""Telemetry: one JSON line per event on stdout, where Render logs and log streams pick it up.

Nothing reads these back to decide what a run does. Follow-ups such as a spend ledger hook in here (D22).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["run.finished", "agent.finished"]


@dataclass(frozen=True)
class EventIds:
    """Stable identifiers shared by every event emitted within one claimed run."""

    trace_id: str
    run_id: str
    repo: str
    subject: str


@dataclass(frozen=True)
class FactoryEvent:
    """How a task run or agent run ended."""

    trace_id: str
    """The root task run."""
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    run_id: str | None = None
    """The run the event describes."""
    repo: str | None = None
    """ "owner/name" """
    subject: str | None = None
    """ "issue#12" or "pr#34" """


def emit(event: FactoryEvent) -> None:
    """Writes the event as one JSON line. Never raises."""
    ids = {"trace_id": event.trace_id, "run_id": event.run_id, "repo": event.repo, "subject": event.subject}
    record = {"level": "info", "event": event.type, **{k: v for k, v in ids.items() if v is not None}, **event.payload}
    try:
        line = json.dumps(record, allow_nan=False)
    except (TypeError, ValueError) as error:
        warn("event dropped", event=event.type, error=str(error))
        return
    print(line, flush=True)


def warn(msg: str, **fields: Any) -> None:
    """One JSON line on stderr, for failures worth an operator's attention that don't fail the run."""
    try:
        line = json.dumps({"level": "warn", "msg": msg, **fields}, default=str)
    except (TypeError, ValueError):
        line = json.dumps({"level": "warn", "msg": msg})
    print(line, file=sys.stderr, flush=True)
