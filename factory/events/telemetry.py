from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["run.finished", "agent.finished"]


@dataclass(frozen=True)
class EventIds:
    trace_id: str
    run_id: str
    repo: str
    subject: str


@dataclass(frozen=True)
class FactoryEvent:
    trace_id: str
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    run_id: str | None = None
    repo: str | None = None
    subject: str | None = None


def emit(event: FactoryEvent) -> None:
    ids = {"trace_id": event.trace_id, "run_id": event.run_id, "repo": event.repo, "subject": event.subject}
    record = {"level": "info", "event": event.type, **{k: v for k, v in ids.items() if v is not None}, **event.payload}
    try:
        line = json.dumps(record, allow_nan=False)
    except (TypeError, ValueError) as error:
        warn("event dropped", event=event.type, error=str(error))
        return
    print(line, flush=True)


def warn(msg: str, **fields: Any) -> None:
    try:
        line = json.dumps({"level": "warn", "msg": msg, **fields}, default=str)
    except (TypeError, ValueError):
        line = json.dumps({"level": "warn", "msg": msg})
    print(line, file=sys.stderr, flush=True)
