"""Per-run accounting for model calls and observable async HTTP attempts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

RequestKind = Literal["conversation", "retry", "summary"]
KINDS: tuple[RequestKind, ...] = ("conversation", "retry", "summary")


@dataclass
class _Counts:
    calls: int = 0
    calls_with_http: int = 0
    http_attempts: int = 0
    sdk_retry_http_attempts: int = 0
    calls_with_usage: int = 0
    usage: Counter[str] = field(default_factory=Counter)


class ModelRequestStats:
    def __init__(self) -> None:
        self._by_kind = {kind: _Counts() for kind in KINDS}

    @contextmanager
    def collect(self) -> Iterator[None]:
        token = _active_stats.set(self)
        parent = _active_request.set(None)
        try:
            yield
        finally:
            _active_request.reset(parent)
            _active_stats.reset(token)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            kind: {
                "calls": counts.calls,
                "http_attempts_observed": counts.http_attempts,
                "sdk_retry_http_attempts_observed": counts.sdk_retry_http_attempts,
                "calls_without_http_observations": counts.calls - counts.calls_with_http,
                "calls_without_usage": counts.calls - counts.calls_with_usage,
                "reported_usage": dict(counts.usage),
            }
            for kind, counts in self._by_kind.items()
        }


@dataclass
class _Request:
    counts: _Counts
    saw_http: bool = False
    saw_usage: bool = False


_active_stats: ContextVar[ModelRequestStats | None] = ContextVar("model_request_stats", default=None)
_active_request: ContextVar[_Request | None] = ContextVar("active_model_request", default=None)


@contextmanager
def track_model_request(kind: RequestKind) -> Iterator[None]:
    stats = _active_stats.get()
    if stats is None:
        yield
        return
    counts = stats._by_kind[kind]
    counts.calls += 1
    token = _active_request.set(_Request(counts))
    try:
        yield
    finally:
        _active_request.reset(token)


def record_model_usage(usage: Mapping[str, int] | None) -> None:
    request = _active_request.get()
    if not usage or request is None or request.saw_usage:
        return
    request.saw_usage = True
    request.counts.calls_with_usage += 1
    request.counts.usage.update({key: value for key, value in usage.items() if type(value) is int})


async def observe_http_attempt(request: Any) -> None:
    """HTTPX hook. Only counts attempts and SDK retry markers, without retaining request data."""
    active = _active_request.get()
    if active is None:
        return
    if not active.saw_http:
        active.saw_http = True
        active.counts.calls_with_http += 1
    active.counts.http_attempts += 1
    retry = request.headers.get("x-stainless-retry-count", "")
    if retry.isascii() and retry.isdigit() and retry.lstrip("0"):
        active.counts.sdk_retry_http_attempts += 1
