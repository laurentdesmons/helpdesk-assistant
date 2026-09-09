"""Escalation store — where flagged requests wait for a human (README §6).

Phase 0 ships an on-disk JSON store for local dev. Phase 5 adds an Azure Table
implementation behind the same interface; ``get_escalation_store`` switches on
``Settings.escalation_store``.

The store is append-and-update by ``request_id`` (the natural key). Writes are
serialized with a file lock and made atomic with a temp-file rename so a crashed
run can't leave a half-written file.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from filelock import FileLock

from helpdesk.config import Settings
from helpdesk.contracts import Category, EscalationRecord, EscalationStatus


class EscalationStore(ABC):
    @abstractmethod
    async def record(self, rec: EscalationRecord) -> None:
        """Insert, or replace the existing record with the same ``request_id``."""

    @abstractmethod
    async def get(self, request_id: str) -> EscalationRecord | None: ...

    @abstractmethod
    async def list(
        self,
        *,
        status: EscalationStatus | None = None,
        category: Category | None = None,
    ) -> list[EscalationRecord]: ...

    @abstractmethod
    async def update_status(self, request_id: str, status: EscalationStatus) -> EscalationRecord:
        """Transition an existing record; raise ``KeyError`` if it's missing."""


class JSONFileEscalationStore(EscalationStore):
    """Single-file JSON store: ``{request_id: EscalationRecord}``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self._path) + ".lock")

    # --- sync core (runs under the file lock, off the event loop) ---------
    def _read(self) -> dict[str, EscalationRecord]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text("utf-8") or "{}")
        return {k: EscalationRecord.model_validate(v) for k, v in raw.items()}

    def _write(self, data: dict[str, EscalationRecord]) -> None:
        payload = {k: v.model_dump(mode="json") for k, v in data.items()}
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _mutate(self, fn):  # type: ignore[no-untyped-def]
        with self._lock:
            data = self._read()
            result = fn(data)
            self._write(data)
            return result

    # --- async interface -------------------------------------------------
    async def record(self, rec: EscalationRecord) -> None:
        await asyncio.to_thread(self._mutate, lambda data: data.__setitem__(rec.request_id, rec))

    async def get(self, request_id: str) -> EscalationRecord | None:
        data = await asyncio.to_thread(lambda: self._read())
        return data.get(request_id)

    async def list(
        self,
        *,
        status: EscalationStatus | None = None,
        category: Category | None = None,
    ) -> list[EscalationRecord]:
        data = await asyncio.to_thread(lambda: self._read())
        cat = category.value if isinstance(category, Category) else category
        out = [
            r
            for r in data.values()
            if (status is None or r.status == status) and (cat is None or r.category == cat)
        ]
        return sorted(out, key=lambda r: r.created_at)

    async def update_status(self, request_id: str, status: EscalationStatus) -> EscalationRecord:
        def _apply(data: dict[str, EscalationRecord]) -> EscalationRecord:
            if request_id not in data:
                raise KeyError(request_id)
            data[request_id] = data[request_id].model_copy(update={"status": status})
            return data[request_id]

        return await asyncio.to_thread(self._mutate, _apply)


def get_escalation_store(settings: Settings) -> EscalationStore:
    if settings.escalation_store == "json":
        return JSONFileEscalationStore(settings.escalation_store_path)
    raise NotImplementedError(
        f"escalation_store={settings.escalation_store!r} is not available yet "
        "(Azure Table store lands in Phase 5)."
    )
