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


def delegation(name="dev.internal.local", ns="ns1.dev.internal.local",
               glue="10.0.0.53"):
    return {"name": name, "ns": ns, "glue": glue}


def test_delegation_validation_rules():
    from app.models import ZonePayload

    def payload(rows, **kwargs):
        body = {"records": [{"name": n, "ip": ip} for n, ip in rows]}
        body.update(kwargs)
        return ZonePayload.model_validate(body)

    base = [("db.internal.local", "10.0.0.11"),
            ("web.internal.local", "10.0.0.21")]

    ok = payload(base, delegation=delegation())
    assert ok.delegation_dict() == delegation()
    # Alias names are accepted for the delegation object.
    alias = payload(base, delegation={
        "subzone": "dev.internal.local",
        "ns_name": "ns1.dev.internal.local",
        "glue_ip": "10.0.0.53",
    })
    assert alias.delegation_dict() == delegation()
    # Relative/trailing-dot/case forms normalise to the same canonical.
    canonical = payload(base, delegation={
        "name": "DEV.internal.local.",
        "ns": "NS1.Dev.Internal.Local.",
        "glue": "10.0.0.53",
    })
    assert canonical.delegation_dict() == delegation()

    # Nested aliases are accepted too.
    dict_alias = payload(base, subzone=delegation())
    assert dict_alias.delegation_dict() == delegation()

    # A plain A-only zone still works (delegation is optional).
    assert payload(base).delegation is None

    # Nested object plus flat fields is ambiguous and must be refused.
    for extra in ({"ns": "ns1.dev.internal.local"},
                  {"subzone": "dev.internal.local"},
                  {"subzone": delegation()}):
        with pytest.raises(Exception):
            payload(base, delegation=delegation(), **extra)

    bad_cases = [
        dict(delegation=delegation(name="dev.other.example")),
        dict(delegation=delegation(name="internal.local")),
        dict(delegation=delegation(ns="ns1.internal.local")),
        dict(delegation=delegation(ns="ns1.other.example")),
        dict(delegation=delegation(glue="fe80::53")),
        dict(delegation=delegation(glue="not-an-ip")),
        dict(delegation={"name": "dev.internal.local"}),
    ]
    for kwargs in bad_cases:
        with pytest.raises(Exception):
            payload(base, **kwargs)

    # Parent A data must not fall inside the delegated child.
    for intruder in ("dev.internal.local", "www.dev.internal.local"):
        with pytest.raises(Exception):
            payload(base + [(intruder, "10.0.0.99")],
                    delegation=delegation())


def test_delegation_is_stored_with_one_version(storage):
    plain = [rec("db.internal.local", "10.0.0.1")]
    serial, _ = storage.storage.next_publish(plain)
    assert serial == 1
    active, records, stored, _ = storage.storage.get_active_snapshot()
    assert active == 1 and records == plain and stored is None

    serial, _ = storage.storage.next_publish(
        [rec("web.internal.local", "10.0.0.2")], delegation()
    )
    active, records, stored, _ = storage.storage.get_active_snapshot()
    assert active == 2
    assert records == [rec("web.internal.local", "10.0.0.2")]
    assert stored == delegation()
    # The DNS-facing read returns the same snapshot.
    assert storage.storage.get_active_zone() == (2, records, delegation())

    # A stale transfer carrying a delegation must not change the boundary.
    assert storage.storage.commit_version(
        1, [rec("db.internal.local", "10.9.9.9")], "late",
        delegation(glue="10.9.9.9"),
    ) is False
    assert storage.storage.get_active_zone()[2] == delegation()

    # A newer version without delegation removes the child delegation.
    storage.storage.commit_version(3, plain, "t3", None)
    assert storage.storage.get_active_zone() == (3, plain, None)


def test_resolver_referral_uses_one_snapshot(storage, monkeypatch):
    """Resolver-level checks for the non-authoritative referral."""
    from dnslib import DNSRecord, QTYPE, RCODE

    from app import dns_server as dns_mod

    # The fixture reloads the storage module; keep the resolver bound to it.
    monkeypatch.setattr(dns_mod, "storage", storage.storage)

    def query(name, qtype="A"):
        request = DNSRecord.question(name, qtype)
        reply = dns_mod.ZoneResolver().resolve(request, None)
        return reply

    records = [
        rec("db.internal.local", "10.0.0.1"),
        rec("internal.local", "10.0.0.254"),
    ]
    storage.storage.next_publish(records, delegation())

    # Only the exact delegation name is delegated.
    r = query("db.internal.local")
    assert r.header.rcode == RCODE.NOERROR
    assert [str(rr.rdata) for rr in r.rr] == ["10.0.0.1"]

    for name in ("dev.internal.local", "www.dev.internal.local",
                 "a.b.dev.internal.local"):
        for qtype in ("A", "AAAA", "NS", "TXT"):
            r = query(name, qtype)
            assert r.header.rcode == RCODE.NOERROR, (name, qtype)
            assert r.header.aa == 0, (name, qtype)
            assert len(r.rr) == 0, (name, qtype)
            assert [str(rr.rname) for rr in r.auth] == \
                ["dev.internal.local."], (name, qtype)
            assert [str(rr.rdata) for rr in r.auth] == \
                ["ns1.dev.internal.local."], (name, qtype)
            assert [(str(rr.rname), str(rr.rdata)) for rr in r.ar] == \
                [("ns1.dev.internal.local.", "10.0.0.53")], (name, qtype)

    # A name under the delegated child that also exists as parent A data
    # must never be answered from that stale A record.
    storage.storage.next_publish(
        records + [rec("x.dev.internal.local", "10.9.9.9")], delegation()
    )
    # (Direct storage write, bypassing publish validation, to model an old
    # parent A record that predates the delegation.)
    r = query("x.dev.internal.local")
    assert len(r.rr) == 0 and len(r.auth) == 1
    assert [str(rr.rdata) for rr in r.ar] == ["10.0.0.53"]

    # Removing the delegation makes the child authoritative (NXDOMAIN).
    storage.storage.next_publish(records, None)
    r = query("dev.internal.local")
    assert r.header.rcode == RCODE.NXDOMAIN
    assert not r.auth and not r.ar


def test_sync_rejects_conflicting_or_malformed_payload(storage):
    from app.sync import _parse_zone

    base = {
        "zone": "internal.local",
        "serial": 4,
        "published_at": "2026-11-17T00:00:00Z",
        "records": [rec("db.internal.local", "10.0.0.1")],
        "delegation": delegation(),
    }
    good, err = _parse_zone(base)
    assert err is None and good["delegation"] == delegation()
    assert good["records"] == [rec("db.internal.local", "10.0.0.1")]

    bad_payloads = [
        # Parent A data inside the delegated child.
        {**base, "records": [rec("x.dev.internal.local", "10.9.9.9")]},
        # Child name equal to the parent zone.
        {**base, "delegation": delegation(name="internal.local")},
        # Nameserver outside the delegated child.
        {**base, "delegation": delegation(ns="ns1.internal.local")},
        # Glue is not IPv4.
        {**base, "delegation": delegation(glue="fe80::1")},
        # Wrong zone in the payload.
        {**base, "zone": "other.local"},
        # Zone field present but not a string.
        {**base, "zone": 7},
        # Missing/invalid required fields.
        {**base, "records": None},
        {**base, "published_at": None},
        {"serial": 4, "published_at": "t", "records": []},
        # Not a dict at all.
        [],
    ]
    for payload in bad_payloads:
        validated, error = _parse_zone(payload)
        assert validated is None and error, payload
    # A payload omitting the zone (old primary) is still accepted.
    without_zone = {k: v for k, v in base.items() if k != "zone"}
    validated, error = _parse_zone(without_zone)
    assert error is None and validated["delegation"] == delegation()
    # None of the rejected payloads touched the active version.
    assert storage.storage.get_active_zone() == (0, [], None)


def test_child_a_record_valid_again_after_delegation_removed():
    from app.models import ZonePayload

    body = {"records": [
        {"name": "x.dev.internal.local", "ip": "10.0.0.9"},
        {"name": "db.internal.local", "ip": "10.0.0.1"},
    ]}
    with pytest.raises(Exception):
        ZonePayload.model_validate({**body, "delegation": delegation()})
    ok = ZonePayload.model_validate(body)
    assert ok.delegation is None


def test_no_answer_mixes_two_delegations_during_switch(storage, monkeypatch):
    """Concurrent reads during repeated version switches must see exactly
    one complete boundary per reply."""
    import threading

    from dnslib import DNSRecord, RCODE

    from app import dns_server as dns_mod

    monkeypatch.setattr(dns_mod, "storage", storage.storage)

    variants = [
        (delegation("dev.internal.local", "ns1.dev.internal.local",
                    "10.0.0.53"),
         "10.0.0.11"),
        (delegation("ops.internal.local", "ns.ops.internal.local",
                    "10.0.0.63"),
         "10.0.0.12"),
        (None, "10.0.0.13"),
    ]
    serial = 0
    for deleg, ip in variants:
        serial, _ = storage.storage.next_publish(
            [rec("db.internal.local", ip)], deleg
        )
    base_serial = serial
    glue_by_child = {
        "dev.internal.local": "10.0.0.53",
        "ops.internal.local": "10.0.0.63",
    }
    stop = threading.Event()
    seen_bad = []
    seen_replies = set()

    def reader():
        resolver = dns_mod.ZoneResolver()
        while not stop.is_set():
            for name in ("x.dev.internal.local", "x.ops.internal.local"):
                reply = resolver.resolve(DNSRecord.question(name), None)
                shape = (
                    reply.header.rcode,
                    tuple(str(rr.rname) for rr in reply.auth),
                    tuple(str(rr.rdata) for rr in reply.auth),
                    tuple((str(rr.rname), str(rr.rdata)) for rr in reply.ar),
                )
                if reply.header.rcode == RCODE.NXDOMAIN:
                    ok = not reply.auth and not reply.ar
                else:
                    ok = (len(reply.auth) == 1 and len(reply.ar) == 1
                          and str(reply.auth[0].rname).rstrip(".")
                          in ("dev.internal.local", "ops.internal.local")
                          and str(reply.ar[0].rdata) == glue_by_child[
                              str(reply.auth[0].rname).rstrip(".")])
                if not ok:
                    seen_bad.append(shape)
                seen_replies.add(shape)

    def writer():
        n = base_serial
        while not stop.is_set():
            for deleg, ip in variants:
                n += 1
                storage.storage.commit_version(
                    n, [rec("db.internal.local", ip)], "t", deleg
                )

    threads = [threading.Thread(target=reader) for _ in range(3)]
    w = threading.Thread(target=writer)
    for t in threads:
        t.start()
    w.start()
    import time
    time.sleep(1.0)
    stop.set()
    w.join()
    for t in threads:
        t.join()
    assert seen_bad == []
    assert len(seen_replies) >= 2


def test_referral_never_answers_from_parent_a_data(storage, monkeypatch):
    """Even if an older version left A data at/below the child, the current
    delegation must win and produce only the referral."""
    from dnslib import DNSRecord

    from app import dns_server as dns_mod

    monkeypatch.setattr(dns_mod, "storage", storage.storage)
    stale = [
        rec("dev.internal.local", "10.9.9.1"),
        rec("deep.dev.internal.local", "10.9.9.2"),
        rec("db.internal.local", "10.0.0.11"),
    ]
    # Write the version with records directly (no delegation yet).
    storage.storage.next_publish(stale, None)
    resolver = dns_mod.ZoneResolver()
    before = resolver.resolve(DNSRecord.question("dev.internal.local"), None)
    assert [str(rr.rdata) for rr in before.rr] == ["10.9.9.1"]
    assert before.header.aa == 1

    # Now the child is delegated in a new version; the resolver must not
    # use the A data left above, only the delegation from the same snapshot.
    storage.storage.next_publish(stale, delegation())
    for name, stale_ip in (("dev.internal.local", "10.9.9.1"),
                           ("deep.dev.internal.local", "10.9.9.2")):
        reply = resolver.resolve(DNSRecord.question(name), None)
        assert not reply.rr, name
        assert reply.header.aa == 0, name
        assert [str(rr.rname) for rr in reply.auth] == \
            ["dev.internal.local."], name
        assert [str(rr.rdata) for rr in reply.ar] == ["10.0.0.53"], name
        assert all(str(rr.rdata) != stale_ip for rr in reply.rr)
    # The parent record still resolves.
    reply = resolver.resolve(DNSRecord.question("db.internal.local"), None)
    assert [str(rr.rdata) for rr in reply.rr] == ["10.0.0.11"]

    # Removing the delegation restores authoritative answers for the
    # parent data (whole-zone semantics: the new version is authoritative).
    storage.storage.next_publish(stale, None)
    reply = resolver.resolve(DNSRecord.question("dev.internal.local"), None)
    assert [str(rr.rdata) for rr in reply.rr] == ["10.9.9.1"]
    assert reply.header.aa == 1
