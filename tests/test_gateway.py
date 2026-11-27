from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest

from factory.config import RunName, get_config
from factory.db import Db, Params, Query, Row, Tx
from factory.gateway import GatewayDeps, create_gateway
from factory.runs.thread import StartingRun, start_on_thread
from factory.schemas import TriageInput
from factory.threads import thread_key
from tests.fixtures import REPO

SECRET = "webhook-secret"
ISSUE = thread_key(f"github:{REPO}#issue-12")


def triage_payload() -> dict[str, Any]:
    triage = get_config().labels.triage
    return {
        "action": "labeled",
        "label": {"name": triage},
        "issue": {"number": 12},
        "repository": {"full_name": REPO},
        "installation": {"id": 7},
        "sender": {"login": "maintainer", "type": "User"},
    }


def comment_payload() -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"number": 12},
        "comment": {"id": 99, "body": "Here are the answers."},
        "repository": {"full_name": REPO},
        "installation": {"id": 7},
        "sender": {"login": "maintainer", "type": "User"},
    }


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Unreachable:
    """A database that is down."""

    async def query(self, query: Query, params: Params = None) -> list[Row]:
        raise ConnectionError("database is unreachable")

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        raise ConnectionError("database is unreachable")

    async def aclose(self) -> None:
        return None


class Gateway:
    def __init__(self, db: Db) -> None:
        self.calls: list[tuple[RunName, dict[str, Any], str]] = []
        self.error: Exception | None = None
        app = create_gateway(GatewayDeps(start_run=self.start_run, webhook_secret=SECRET, db=db))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")

    async def start_run(self, run: RunName, input: dict[str, Any], idempotency_key: str) -> str:
        await asyncio.sleep(0)
        self.calls.append((run, input, idempotency_key))
        if self.error:
            raise self.error
        return "trn-123"

    async def webhook(
        self, body: bytes, *, event: str = "issues", delivery: str | None = "delivery-1", signature: str | None = None
    ) -> httpx.Response:
        headers = {
            "content-type": "application/json",
            "x-github-event": event,
            "x-hub-signature-256": signature or sign(body),
        }
        if delivery:
            headers["x-github-delivery"] = delivery
        return await self.client.post("/webhooks/github", content=body, headers=headers)


@pytest.fixture
async def offline() -> AsyncIterator[Gateway]:
    """A gateway whose database is down, for requests that must be answered before it is consulted."""
    gateway = Gateway(Unreachable())
    yield gateway
    await gateway.client.aclose()


@pytest.fixture
async def gateway(db: Db) -> AsyncIterator[Gateway]:
    gateway = Gateway(db)
    yield gateway
    await gateway.client.aclose()


async def test_serves_health_and_ready(offline: Gateway) -> None:
    assert (await offline.client.get("/health")).status_code == 200
    ready = await offline.client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


async def test_tells_a_browser_the_headless_gateway_is_running_without_revealing_its_secret(offline: Gateway) -> None:
    res = await offline.client.get("/")
    assert res.status_code == 200
    assert "software-factory gateway is running" in res.text
    assert "no web UI" in res.text
    assert SECRET not in res.text
    assert offline.calls == []


async def test_rejects_a_bad_signature_before_parsing_the_body(
    offline: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed: list[object] = []
    real_loads = json.loads

    def spy(s: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(s)
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr("factory.gateway.app.json.loads", spy)
    res = await offline.webhook(b"{not json", signature=sign(b"{not json", "wrong"))
    assert res.status_code == 401
    assert parsed == []
    assert offline.calls == []


async def test_rejects_a_missing_signature(offline: Gateway) -> None:
    body = json.dumps(triage_payload()).encode()
    res = await offline.client.post(
        "/webhooks/github", content=body, headers={"x-github-event": "issues", "x-github-delivery": "d"}
    )
    assert res.status_code == 401
    assert offline.calls == []


async def test_rejects_webhook_bodies_over_256_kb_before_starting_work(offline: Gateway) -> None:
    body = json.dumps({"padding": "x" * 256 * 1024}).encode()
    assert (await offline.webhook(body)).status_code == 413
    assert offline.calls == []


async def test_returns_502_and_starts_nothing_when_the_database_is_down(
    offline: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    res = await offline.webhook(json.dumps(triage_payload()).encode())
    assert res.status_code == 502
    assert res.json() == {"error": "could not record delivery"}
    assert offline.calls == []
    logged = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert logged["msg"] == "could not record delivery"


@pytest.mark.postgres
async def test_records_the_decision_then_starts_the_run_keyed_by_the_delivery(gateway: Gateway, db: Db) -> None:
    res = await gateway.webhook(json.dumps(triage_payload()).encode())
    assert res.status_code == 202
    assert res.json() == {"run_id": "trn-123", "run": "triage"}
    [(run, input, key)] = gateway.calls
    assert (run, key) == ("triage", "delivery-1")
    TriageInput.model_validate(input)
    recorded = await db.query("select stream, thread, payload from events")
    assert recorded == [
        {
            "stream": "delivery:github:delivery-1",
            "thread": ISSUE,
            "payload": {
                "event": "issues",
                "repo": REPO,
                "task": "triage",
                "input": input,
                "idempotency_key": "delivery-1",
            },
        }
    ]


@pytest.mark.postgres
async def test_a_redelivery_replays_the_recorded_decision(gateway: Gateway, db: Db) -> None:
    body = json.dumps(comment_payload()).encode()
    await start_on_thread(db, StartingRun(ISSUE, "trn-root", "trn-root", "triage", "Triage", "maintainer", "replace"))
    await db.query("update runs set waiting = 'reply' where run_id = 'trn-root'")

    first = await gateway.webhook(body, event="issue_comment")
    await db.query("update runs set waiting = null where run_id = 'trn-root'")
    second = await gateway.webhook(body, event="issue_comment")

    assert first.status_code == second.status_code == 202
    assert [call[0] for call in gateway.calls] == ["triage", "triage"]
    assert gateway.calls[0] == gateway.calls[1]
    assert gateway.calls[0][1]["reply_to"] == "trn-root"


@pytest.mark.postgres
async def test_a_comment_on_a_thread_that_is_not_waiting_is_recorded_as_ignored(gateway: Gateway, db: Db) -> None:
    res = await gateway.webhook(json.dumps(comment_payload()).encode(), event="issue_comment")
    assert res.json() == {"ignored": "thread is not waiting for a reply"}
    assert gateway.calls == []
    [recorded] = await db.query("select payload from events")
    assert recorded["payload"] == {"event": "issue_comment", "ignored": "thread is not waiting for a reply"}


@pytest.mark.postgres
async def test_when_starting_fails_the_decision_stays_and_a_redelivery_starts_the_same_run(
    gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    body = json.dumps(triage_payload()).encode()
    gateway.error = RuntimeError("render down")
    res = await gateway.webhook(body)
    assert res.status_code == 502
    logged = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert (logged["msg"], logged["delivery_id"], logged["error"]) == (
        "could not start run",
        "delivery-1",
        "render down",
    )

    gateway.error = None
    assert (await gateway.webhook(body)).status_code == 202
    assert gateway.calls[0] == gateway.calls[1]


@pytest.mark.postgres
async def test_concurrent_deliveries_with_one_id_record_one_decision(gateway: Gateway, db: Db) -> None:
    body = json.dumps(triage_payload()).encode()
    responses = await asyncio.gather(gateway.webhook(body), gateway.webhook(body))
    assert [r.status_code for r in responses] == [202, 202]
    assert len(await db.query("select 1 from events")) == 1
    assert {call[2] for call in gateway.calls} == {"delivery-1"}
