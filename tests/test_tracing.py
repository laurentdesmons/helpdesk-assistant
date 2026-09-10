"""tracing.build_tracer / configure_tracing — hermetic, no network."""

from __future__ import annotations

import pytest

from helpdesk.config import Settings
from helpdesk.tracing import build_tracer, configure_tracing

pytest.importorskip("langchain_azure_ai")


def test_build_tracer_disabled_returns_none() -> None:
    assert build_tracer(Settings(_env_file=None, tracing_enabled=False)) is None  # type: ignore[call-arg]


def test_build_tracer_offline_without_connection_string() -> None:
    # No App Insights configured -> a spans-only tracer, constructed without any
    # network call (auto_configure_azure_monitor=False).
    tracer = build_tracer(Settings(_env_file=None))  # type: ignore[call-arg]
    assert tracer is not None


def test_configure_tracing_is_idempotent_and_offline() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    configure_tracing(s)
    configure_tracing(s)  # no raise on second call

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    assert isinstance(trace.get_tracer_provider(), TracerProvider)


def test_configure_tracing_noop_when_disabled() -> None:
    configure_tracing(Settings(_env_file=None, tracing_enabled=False))  # type: ignore[call-arg]
