"""Rich console logging with OpenTelemetry trace-id enrichment.

``configure_logging`` is called once by every entrypoint (scripts, agent hosts).
Every log line carries the active ``trace_id`` / ``span_id`` when there is one,
so local logs line up with the spans that show in Foundry Observability later.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from rich.logging import RichHandler

# Third-party loggers that are noisy at DEBUG/INFO — keep them at WARNING unless
# the root level is explicitly DEBUG.
_NOISY = (
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "httpx",
    "httpcore",
    "openai",
)


class _TraceContextFilter(logging.Filter):
    """Attach ``trace_id`` / ``span_id`` to every record (``-`` when no span)."""

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx and ctx.is_valid:
            record.trace_id = format(ctx.trace_id, "032x")
            record.span_id = format(ctx.span_id, "016x")
        else:
            record.trace_id = "-"
            record.span_id = "-"
        return True


def configure_logging(level: str = "INFO", *, quiet_third_party: bool = True) -> None:
    handler = RichHandler(
        rich_tracebacks=True,
        show_path=False,
        omit_repeated_times=False,
    )
    handler.addFilter(_TraceContextFilter())

    logging.basicConfig(
        level=level.upper(),
        format="%(message)s  [dim]trace=%(trace_id)s[/dim]",
        datefmt="%H:%M:%S",
        handlers=[handler],
        force=True,
    )

    if quiet_third_party and level.upper() != "DEBUG":
        for name in _NOISY:
            logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
