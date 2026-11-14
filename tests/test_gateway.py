from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.config import RunName, get_config
from factory.gateway import GatewayDeps, create_gateway
from factory.schemas import TriageInput
from tests.fixtures import REPO

SECRET = "webhook-secret"


def triage_payload() -> dict[str, Any]:
    triage = get_config().labels.triage
    return {
        "action": "labeled",
        "label": {"name": triage},
        "issue": {"number": 12, "labels": [{"name": triage}]},
        "repository": {"full_name": REPO},
        "installation": {"id": 7},
        "sender": {"login": "maintainer", "type": "User"},
    }


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[RunName, dict[str, Any], str]] = []
        self.error: Exception | None = None
        self.client = TestClient(create_gateway(GatewayDeps(start_run=self.start_run, webhook_secret=SECRET)))

    async def start_run(self, run: RunName, input: dict[str, Any], idempotency_key: str) -> str:
        self.calls.append((run, input, idempotency_key))
        if self.error:
            raise self.error
        return "trn-123"

    def webhook(
        self, body: bytes, *, event: str = "issues", delivery: str | None = "delivery-1", signature: str | None = None
    ) -> Any:
        headers = {
            "content-type": "application/json",
            "x-github-event": event,
            "x-hub-signature-256": signature or sign(body),
        }
        if delivery:
            headers["x-github-delivery"] = delivery
        return self.client.post("/webhooks/github", content=body, headers=headers)


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


def test_serves_health_and_ready(gateway: Gateway) -> None:
    assert gateway.client.get("/health").status_code == 200
    ready = gateway.client.get("/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_tells_a_browser_the_headless_gateway_is_running_without_revealing_its_secret(gateway: Gateway) -> None:
    res = gateway.client.get("/")
    assert res.status_code == 200
    assert "software-factory gateway is running" in res.text
    assert "no web UI" in res.text
    assert SECRET not in res.text
    assert gateway.calls == []


def test_rejects_a_bad_signature_before_parsing_the_body(gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed: list[object] = []
    real_loads = json.loads

    def spy(s: Any, *args: Any, **kwargs: Any) -> Any:
        parsed.append(s)
        return real_loads(s, *args, **kwargs)

    monkeypatch.setattr("factory.gateway.app.json.loads", spy)
    res = gateway.webhook(b"{not json", signature=sign(b"{not json", "wrong"))
    assert res.status_code == 401
    assert parsed == []
    assert gateway.calls == []


def test_rejects_a_missing_signature(gateway: Gateway) -> None:
    body = json.dumps(triage_payload()).encode()
    res = gateway.client.post(
        "/webhooks/github", content=body, headers={"x-github-event": "issues", "x-github-delivery": "d"}
    )
    assert res.status_code == 401
    assert gateway.calls == []


def test_rejects_webhook_bodies_over_256_kb_before_starting_work(gateway: Gateway) -> None:
    body = json.dumps({"padding": "x" * 256 * 1024}).encode()
    assert gateway.webhook(body).status_code == 413
    assert gateway.calls == []


def test_starts_the_routed_run_with_the_delivery_id_as_idempotency_key(gateway: Gateway) -> None:
    res = gateway.webhook(json.dumps(triage_payload()).encode())
    assert res.status_code == 202
    assert res.json() == {"run_id": "trn-123", "run": "triage"}
    assert len(gateway.calls) == 1
    run, input, key = gateway.calls[0]
    assert run == "triage"
    assert key == "delivery-1"
    TriageInput.model_validate(input)


def test_returns_502_and_logs_why_when_starting_fails(gateway: Gateway, capsys: pytest.CaptureFixture[str]) -> None:
    gateway.error = RuntimeError("render down")
    res = gateway.webhook(json.dumps(triage_payload()).encode())
    assert res.status_code == 502
    logged = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert logged["msg"] == "could not start run"
    assert logged["delivery_id"] == "delivery-1"
    assert logged["error"] == "render down"
