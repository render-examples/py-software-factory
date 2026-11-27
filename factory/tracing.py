from __future__ import annotations

import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import logfire
from logfire.types import StringValue
from opentelemetry import trace
from opentelemetry.trace import format_trace_id

if TYPE_CHECKING:
    from fastapi import FastAPI

TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
FLUSH_TIMEOUT_MILLIS = 5_000
GATEWAY_UNTRACED_URLS = ("/health", "/ready")

_CREDENTIALS = re.compile(
    r"\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bsk-ant-[A-Za-z0-9_-]{20,}"
    r"|\brnd_[A-Za-z0-9]{20,}"
    r"|\bpylf_v\d+_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def mask_credentials(text: str) -> str:
    return _CREDENTIALS.sub("[credential redacted]", text)


def _transform(item: StringValue) -> str:
    return mask_credentials(item.value)


def configure(service_name: str) -> None:
    repository = os.environ.get("RENDER_GIT_REPO_SLUG", "").strip()
    revision = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    logfire.configure(
        service_name=service_name,
        send_to_logfire="if-token-present",
        console=False,
        distributed_tracing=False,
        scrubbing=logfire.ScrubbingOptions(transform_string=_transform),
        code_source=(
            logfire.CodeSource(repository=f"https://github.com/{repository}", revision=revision)
            if repository and revision
            else None
        ),
    )


def instrument_workflows(*, include_content: bool) -> None:
    logfire.instrument_pydantic_ai(include_content=include_content)
    logfire.instrument_httpx()
    logfire.instrument_asyncpg()


def instrument_gateway(app: FastAPI) -> None:
    logfire.instrument_fastapi(app, excluded_urls=GATEWAY_UNTRACED_URLS)
    logfire.instrument_httpx()
    logfire.instrument_asyncpg()


def trace_link() -> str | None:
    project = os.environ.get("LOGFIRE_PROJECT_URL", "").strip().rstrip("/")
    parts = urlsplit(project)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        return None
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    query = urlencode({"q": f"trace_id='{format_trace_id(context.trace_id)}'"})
    return f"{project}?{query}"


def carrier() -> dict[str, str] | None:
    traceparent = logfire.get_context().get("traceparent")
    return {"traceparent": traceparent} if traceparent and TRACEPARENT.fullmatch(traceparent) else None


@contextmanager
def attached(context: Mapping[str, str] | None) -> Generator[None]:
    if context is None:
        yield
        return
    with logfire.attach_context(dict(context)):
        yield


def flush() -> None:
    with suppress(Exception):
        logfire.force_flush(timeout_millis=FLUSH_TIMEOUT_MILLIS)
