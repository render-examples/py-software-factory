from __future__ import annotations

import asyncio
import ssl

import pytest

import scripts.database as database
from factory.db.pool import connect_options
from scripts.common import print_checks
from scripts.database import check_database, database_checks, url_secrets

URL = "postgresql://factory_user:p%40ss-w0rd!@dpg-abc123.oregon-postgres.render.com:5432/factory?sslmode=verify-full"
CREDENTIALS = ("p%40ss-w0rd!", "p@ss-w0rd!", "factory_user", "factory_user:p%40ss-w0rd!", URL)


class FakeProbe:
    def __init__(
        self,
        *,
        ping_error: BaseException | None = None,
        pending: list[str] | None = None,
        pending_error: BaseException | None = None,
        ping_delay: float = 0,
    ) -> None:
        self.ping_error = ping_error
        self.pending = pending or []
        self.pending_error = pending_error
        self.ping_delay = ping_delay
        self.closed = False

    async def ping(self) -> None:
        if self.ping_delay:
            await asyncio.sleep(self.ping_delay)
        if self.ping_error is not None:
            raise self.ping_error

    async def pending_migrations(self) -> list[str]:
        if self.pending_error is not None:
            raise self.pending_error
        return self.pending

    async def aclose(self) -> None:
        self.closed = True


def _leaks(text: str) -> list[str]:
    return [secret for secret in CREDENTIALS if secret in text]


def test_failing_connection_output_contains_no_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    error = RuntimeError(
        f'connection to "{URL}" failed: FATAL:  password authentication failed for user "factory_user"\n'
        "password was p@ss-w0rd!"
    )
    probe = FakeProbe(ping_error=error)
    checks = check_database(URL, open_db=lambda url: probe)

    assert [(check.name, check.ok, check.required) for check in checks] == [
        ("database connects", False, True),
        ("migrations current", False, False),
    ]
    assert checks[0].detail.startswith("RuntimeError: ")
    assert "password authentication failed" in checks[0].detail
    assert "\n" not in checks[0].detail
    assert probe.closed
    print_checks(checks)
    print_checks(checks, json_output=True)
    captured = capsys.readouterr()
    assert not _leaks(captured.out + captured.err)


def test_opener_failure_is_redacted() -> None:
    def explode(url: str) -> FakeProbe:
        raise ValueError(f"invalid connection option in {url!r}")

    [connects, _] = check_database(URL, open_db=explode)
    assert not connects.ok
    assert not _leaks(connects.detail)


def test_pending_migrations_are_a_warning() -> None:
    probe = FakeProbe(pending=["001_event_log.sql", "002_next.sql"])
    connects, migrations = check_database(URL, open_db=lambda url: probe)
    assert connects.ok
    assert not migrations.ok
    assert not migrations.required
    assert "2 pending (001_event_log.sql, 002_next.sql)" in migrations.detail
    assert probe.closed


def test_current_migrations_pass() -> None:
    checks = check_database(URL, open_db=lambda url: FakeProbe())
    assert [(check.name, check.ok) for check in checks] == [("database connects", True), ("migrations current", True)]


def test_migration_query_failure_is_redacted_warning() -> None:
    probe = FakeProbe(pending_error=RuntimeError(f"permission denied for {URL}"))
    _, migrations = check_database(URL, open_db=lambda url: probe)
    assert not migrations.ok
    assert not migrations.required
    assert not _leaks(migrations.detail)


def test_slow_connection_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "TIMEOUT_SECONDS", 0.01)
    probe = FakeProbe(ping_delay=1)
    [connects, _] = check_database(URL, open_db=lambda url: probe)
    assert connects.detail == "timed out after 0.01s"
    assert probe.closed


def test_url_secrets_cover_every_credential_form() -> None:
    secrets = url_secrets(URL)
    for credential in CREDENTIALS:
        assert credential in secrets
    assert "secret-pw" in url_secrets("host=db.internal user=factory password=secret-pw dbname=factory")
    assert "qs-pw" in url_secrets("postgresql://db.internal/factory?user=factory&password=qs-pw")
    assert url_secrets("postgresql:///postgres") == ["postgresql:///postgres"]


def test_database_checks_skip_without_url() -> None:
    [skip] = database_checks({"DATABASE_URL": "  "})
    assert skip.name == "database"
    assert not skip.required
    assert "docs/configuration.md#database" in skip.detail


def test_system_trust_store_urls_become_a_verifying_tls_context() -> None:
    dsn, context = connect_options(
        "postgresql://u:p@dpg-abc.oregon-postgres.render.com/factory?sslmode=verify-full&sslrootcert=system"
    )
    assert dsn == "postgresql://u:p@dpg-abc.oregon-postgres.render.com/factory"
    assert context is not None
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_other_urls_pass_through_to_asyncpg() -> None:
    internal = "postgresql://u:p@dpg-abc-a/factory"
    assert connect_options(internal) == (internal, None)
    external = "postgresql://u:p@host/factory?sslmode=require"
    assert connect_options(external) == (external, None)
