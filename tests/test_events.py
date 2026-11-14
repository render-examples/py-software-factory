from __future__ import annotations

import json

import pytest

from factory.events import FactoryEvent, emit


def test_writes_one_json_line_with_the_event_name_ids_and_payload(capsys: pytest.CaptureFixture[str]) -> None:
    emit(
        FactoryEvent(
            "trn-root",
            "run.finished",
            {"stage": "triage", "state": "done", "outcome": "drafted"},
            run_id="trn-2",
            repo="acme/web",
            subject="issue#12",
        )
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "level": "info",
        "event": "run.finished",
        "trace_id": "trn-root",
        "run_id": "trn-2",
        "repo": "acme/web",
        "subject": "issue#12",
        "stage": "triage",
        "state": "done",
        "outcome": "drafted",
    }


def test_never_raises_even_when_the_payload_cannot_be_serialized(capsys: pytest.CaptureFixture[str]) -> None:
    emit(FactoryEvent("trn-1", "agent.finished", {"cost": object()}))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "event dropped" in captured.err
