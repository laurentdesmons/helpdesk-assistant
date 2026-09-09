"""JSONFileEscalationStore behaviour. Hermetic, uses a tmp path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from helpdesk.config import Settings
from helpdesk.contracts import Category, EscalationRecord
from helpdesk.escalation import JSONFileEscalationStore, get_escalation_store


@pytest.fixture
def store(tmp_path: Path) -> JSONFileEscalationStore:
    return JSONFileEscalationStore(tmp_path / "escalations.json")


def _rec(request_id: str, category: Category, reason: str) -> EscalationRecord:
    return EscalationRecord(
        request_id=request_id,
        category=category,
        escalation_reason=reason,  # type: ignore[arg-type]
    )


async def test_record_and_get(store: JSONFileEscalationStore) -> None:
    rec = _rec("r1", Category.billing, "policy_escalation")
    await store.record(rec)
    got = await store.get("r1")
    assert got is not None
    assert got.request_id == "r1"
    assert got.category == "billing"
    assert got.escalation_reason == "policy_escalation"


async def test_record_replaces_same_request_id(store: JSONFileEscalationStore) -> None:
    await store.record(_rec("r1", Category.support, "not_grounded"))
    await store.record(_rec("r1", Category.support, "low_confidence"))
    got = await store.get("r1")
    assert got is not None and got.escalation_reason == "low_confidence"
    assert len(await store.list()) == 1


async def test_list_filters(store: JSONFileEscalationStore) -> None:
    await store.record(_rec("r1", Category.support, "not_grounded"))
    await store.record(_rec("r2", Category.hr, "not_grounded"))
    await store.record(_rec("r3", Category.billing, "policy_escalation"))

    assert {r.request_id for r in await store.list(category=Category.hr)} == {"r2"}
    assert {r.request_id for r in await store.list(status="flagged")} == {"r1", "r2", "r3"}
    assert await store.list(status="resolved") == []


async def test_update_status(store: JSONFileEscalationStore) -> None:
    await store.record(_rec("r1", Category.support, "not_grounded"))
    updated = await store.update_status("r1", "in_progress")
    assert updated.status == "in_progress"
    assert (await store.get("r1")).status == "in_progress"  # type: ignore[union-attr]

    with pytest.raises(KeyError):
        await store.update_status("missing", "resolved")


async def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "escalations.json"
    await JSONFileEscalationStore(path).record(_rec("r1", Category.hr, "not_grounded"))
    reopened = await JSONFileEscalationStore(path).get("r1")
    assert reopened is not None and reopened.category == "hr"


async def test_concurrent_writes_do_not_corrupt(store: JSONFileEscalationStore) -> None:
    await asyncio.gather(*(store.record(_rec(f"r{i}", Category.support, "not_grounded")) for i in range(25)))
    assert len(await store.list()) == 25


def test_get_escalation_store_json() -> None:
    s = Settings(escalation_store="json", escalation_store_path=".local/x.json")
    assert isinstance(get_escalation_store(s), JSONFileEscalationStore)


def test_get_escalation_store_table_not_yet() -> None:
    s = Settings(escalation_store="table")
    with pytest.raises(NotImplementedError):
        get_escalation_store(s)
