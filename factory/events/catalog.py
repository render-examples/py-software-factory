"""Every event the log records. A payload's shape is part of the log's contract: change it with a new version."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


P = TypeVar("P", bound=Payload)


@dataclass(frozen=True)
class EventSpec(Generic[P]):
    type: str
    version: int
    payload: type[P]

    def parse(self, data: object) -> P:
        return self.payload.model_validate(data)


def define_event(type: str, version: int, payload: type[P]) -> EventSpec[P]:
    return EventSpec(type, version, payload)


ID_MAX = 200

Id = Annotated[str, Field(min_length=1, max_length=ID_MAX)]
Name = Annotated[str, Field(min_length=1, max_length=100)]
Summary = Annotated[str, Field(max_length=16_000)]
Count = Annotated[int, Field(ge=0)]
FinalState = Literal["done", "failed", "cancelled"]
RunMode = Literal["replace", "continue", "reply"]


class DeliveryReceived(Payload):
    event: Name
    repo: Id | None = None
    task: Name | None = None
    input: dict[str, Any] | None = None
    idempotency_key: Annotated[str, Field(max_length=400)] | None = None
    ignored: Annotated[str, Field(max_length=200)] | None = None


class RunStarted(Payload):
    run_id: Id
    trace_id: Id
    task: Name
    title: Name
    actor: Name
    mode: RunMode


class RunRejected(Payload):
    trace_id: Id
    task: Name
    title: Name
    actor: Name
    reason: Summary


class RunProgress(Payload):
    summary: Summary


class RunAwaitingReply(Payload):
    pass


class RunSandboxAttached(Payload):
    sandbox_id: Id


class Unconfirmed(Payload):
    task_run: Id | None = None
    sandboxes: Annotated[tuple[Id, ...], Field(max_length=50)] = ()


class RunStopped(Payload):
    by: Id
    active: bool
    summary: Summary
    unconfirmed: Unconfirmed | None = None


class RunCleanedUp(Payload):
    by: Id


class Link(Payload):
    label: Annotated[str, Field(max_length=200)]
    url: Annotated[str, Field(max_length=2000)]


class RunFinished(Payload):
    state: FinalState
    outcome: Name
    summary: Summary
    links: Annotated[tuple[Link, ...], Field(max_length=20)] = ()
    duration_ms: Count
    waiting: Literal["reply"] | None = None


delivery_received = define_event("delivery.received", 1, DeliveryReceived)

run_started = define_event("run.started", 1, RunStarted)
run_rejected = define_event("run.rejected", 1, RunRejected)
run_progress = define_event("run.progress", 1, RunProgress)
run_awaiting_reply = define_event("run.awaiting_reply", 1, RunAwaitingReply)
run_sandbox_attached = define_event("run.sandbox_attached", 1, RunSandboxAttached)
run_stopped = define_event("run.stopped", 1, RunStopped)
run_cleaned_up = define_event("run.cleaned_up", 1, RunCleanedUp)
run_finished = define_event("run.finished", 1, RunFinished)
