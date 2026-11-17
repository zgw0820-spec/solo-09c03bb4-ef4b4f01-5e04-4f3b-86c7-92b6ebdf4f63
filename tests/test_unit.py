"""Unit tests for validation and the SQLite version/rollback guarantees."""
import os
import tempfile

import pytest


@pytest.fixture()
def storage(tmp_path, monkeypatch):
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db)
    monkeypatch.setenv("DNS_ZONE", "internal.local")
    monkeypatch.setenv("MAX_RECORDS", "8")
    # Import fresh so settings + storage bind to the temp env.
    import importlib

    from app import config, storage as storage_mod

    importlib.reload(config)
    importlib.reload(storage_mod)
    yield storage_mod
    storage_mod.storage._connect().close()


def rec(name, ip):
    return {"name": name, "ip": ip}


def test_seed_starts_at_serial_zero(storage):
    assert storage.storage.active_serial() == 0
    serial, records, _ = storage.storage.get_active_version()
    assert serial == 0 and records == []


def test_publish_increments_serial_only_on_success(storage):
    serial, _ = storage.storage.next_publish(
        [rec("db.internal.local", "10.0.0.1")]
    )
    assert serial == 1
    serial, _ = storage.storage.next_publish(
        [rec("db.internal.local", "10.0.0.2")]
    )
    assert serial == 2
    # Active version is exactly one immutable snapshot.
    active, records, _ = storage.storage.get_active_version()
    assert active == 2 and records == [rec("db.internal.local", "10.0.0.2")]


def test_stale_transfer_cannot_roll_back(storage):
    storage.storage.next_publish([rec("a.internal.local", "10.0.0.1")])
    storage.storage.commit_version(
        2, [rec("b.internal.local", "10.0.0.2")], "t2"
    )
    assert storage.storage.active_serial() == 2
    # Late arrival of an old complete version.
    applied = storage.storage.commit_version(
        1, [rec("old.internal.local", "10.9.9.9")], "t1-late"
    )
    assert applied is False
    assert storage.storage.active_serial() == 2
    _, records, _ = storage.storage.get_active_version()
    assert records == [rec("b.internal.local", "10.0.0.2")]
    # Re-delivery of the same serial is idempotent and rejected too.
    assert (
        storage.storage.commit_version(
            2, [rec("b.internal.local", "10.0.0.2")], "t2-dup"
        )
        is False
    )


def test_failed_commit_leaves_old_version(storage):
    storage.storage.next_publish([rec("a.internal.local", "10.0.0.1")])
    with pytest.raises(Exception):
        # serial < 1 is invalid; nothing should have changed.
        storage.storage.commit_version(
            0, [rec("x.internal.local", "10.0.0.9")], "bad"
        )
    assert storage.storage.active_serial() == 1


def test_zone_validation_rules():
    from app.models import ZonePayload

    def payload(rows):
        return ZonePayload.model_validate(
            {"records": [{"name": n, "ip": ip} for n, ip in rows]}
        )

    ok = payload(
        [
            ("db.internal.local", "10.0.0.1"),
            ("web.internal.local", "10.0.0.2"),
        ]
    )
    assert len(ok.records) == 2

    with pytest.raises(Exception):
        payload([])  # empty zone
    with pytest.raises(Exception):
        payload([("db.external.example", "10.0.0.1")])  # outside zone
    with pytest.raises(Exception):
        payload([("db.internal.local", "not-an-ip")])
    with pytest.raises(Exception):
        payload([("db.internal.local", "fe80::1")])  # IPv6 not allowed
    with pytest.raises(Exception):
        payload(
            [
                ("db.internal.local", "10.0.0.1"),
                ("db.internal.local", "10.0.0.2"),
            ]
        )  # duplicate name
    with pytest.raises(Exception):
        payload(
            [(f"h{i}.internal.local", f"10.0.0.{i}") for i in range(9)]
        )  # > 8 records

    # Bare apex and unqualified relative names inside the zone are fine.
    apex = payload([("internal.local", "10.0.0.1")])
    assert apex.records[0].name == "internal.local"
    trailing = payload([("svc.internal.local.", "10.0.0.5")])
    assert trailing.records[0].name == "svc.internal.local"
