"""End-to-end test: real primary + backup uvicorn processes + UDP DNS.

Run with the project venv:  .venv/bin/python -I tests/e2e_test.py
Scenarios exercised:
 1. startup on serial 0 -> NXDOMAIN but service stays up
 2. validation rejects bad / >8 record publishes, old version untouched
 3. successful publish increments serial; primary answers via UDP
 4. backup manual sync + background auto-sync; backup answers same version
 5. slow/failed transfer (blackhole upstream): backup keeps answering old
 6. recovery after the broken transfer: catch up to newest version
 7. stale transfer guard: old serial can never roll back the active zone
 8. backup restart: independent DB persists, then auto-catches-up
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dnslib import DNSRecord, RCODE

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

ZONE = "internal.local"
PRIMARY_API = "http://127.0.0.1:18000"
BACKUP_API = "http://127.0.0.1:18001"
PRIMARY_DNS = ("127.0.0.1", 15053)
BACKUP_DNS = ("127.0.0.1", 15054)
BLACKHOLE = "http://127.0.0.1:19999"  # nothing listens -> connection refused
SYNC_INTERVAL = "2"

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=1.5)
            if r.status_code == 200:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.3)
    raise RuntimeError(f"service not ready: {url} ({last})")


def dns_query(server, name: str, qtype: str = "A", timeout: float = 3.0):
    q = DNSRecord.question(name, qtype)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    sock.sendto(q.pack(), server)
    data, _ = sock.recvfrom(65535)
    sock.close()
    return DNSRecord.parse(data)


def start_service(role: str, api_port: int, dns_port: int,
                  db: str, primary_url: str | None) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "DNS_ROLE": role,
            "DNS_ZONE": ZONE,
            "DB_PATH": db,
            "DNS_HOST": "127.0.0.1",
            "DNS_PORT": str(dns_port),
            "API_HOST": "127.0.0.1",
            "API_PORT": str(api_port),
            "SYNC_INTERVAL": SYNC_INTERVAL,
            "HTTP_TIMEOUT": "3",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if primary_url:
        env["PRIMARY_API_URL"] = primary_url
    log = open(DATA / f"{role}.log", "w")
    proc = subprocess.Popen(
        [PY, "-I", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return proc


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def publish(api: str, records) -> httpx.Response:
    return httpx.post(f"{api}/api/zone", json={"records": records}, timeout=5)


def main() -> int:
    for f in ("primary-e2e.db", "backup-e2e.db"):
        for suffix in ("", "-wal", "-shm"):
            p = DATA / (f + suffix)
            if p.exists():
                p.unlink()

    print("[1] start primary + backup (empty, serial 0)")
    primary = start_service(
        "primary", 18000, 15053, str(DATA / "primary-e2e.db"), None
    )
    backup = start_service(
        "backup", 18001, 15054, str(DATA / "backup-e2e.db"), PRIMARY_API
    )
    try:
        wait_http(f"{PRIMARY_API}/health")
        wait_http(f"{BACKUP_API}/health")
        time.sleep(1.0)

        check("both start at serial 0",
              httpx.get(f"{PRIMARY_API}/api/version").json()["serial"] == 0
              and httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 0)
        r = dns_query(PRIMARY_DNS, f"db.{ZONE}")
        check("serial 0 answers NXDOMAIN",
              r.header.rcode == RCODE.NXDOMAIN, str(r.header))

        print("[2] validation failures must not change the zone")
        bad = publish(PRIMARY_API, [])
        check("empty zone rejected (422)", bad.status_code == 422, bad.text)
        bad = publish(PRIMARY_API,
                      [{"name": f"h{i}.other.example", "ip": f"10.0.0.{i}"}
                       for i in range(9)])
        check(">8 records rejected", bad.status_code == 422)
        bad = publish(PRIMARY_API,
                      [{"name": "evil.external.net", "ip": "10.0.0.1"}])
        check("out-of-zone name rejected", bad.status_code == 422)
        bad = publish(PRIMARY_API,
                      [{"name": f"db.{ZONE}", "ip": "203.0.113.1"},
                       {"name": f"db.{ZONE}", "ip": "203.0.113.2"}])
        check("duplicate name rejected", bad.status_code == 422)
        check("still serial 0 after failed publishes",
              httpx.get(f"{PRIMARY_API}/api/version").json()["serial"] == 0)

        print("[3] successful publish increments serial; primary serves UDP")
        resp = publish(PRIMARY_API, [
            {"name": f"db.{ZONE}", "ip": "10.0.0.11"},
            {"name": f"web.{ZONE}", "ip": "10.0.0.21"},
        ])
        check("publish v1 -> 201 serial 1",
              resp.status_code == 201 and resp.json()["serial"] == 1,
              resp.text)
        r = dns_query(PRIMARY_DNS, f"db.{ZONE}")
        ips = [str(rr.rdata) for rr in r.rr]
        check("primary UDP answers v1 record",
              r.header.rcode == RCODE.NOERROR and ips == ["10.0.0.11"],
              str(r))
        r = dns_query(PRIMARY_DNS, f"web.{ZONE}")
        check("primary UDP second name",
              [str(rr.rdata) for rr in r.rr] == ["10.0.0.21"])
        r = dns_query(PRIMARY_DNS, f"missing.{ZONE}")
        check("unknown name NXDOMAIN", r.header.rcode == RCODE.NXDOMAIN)
        r = dns_query(PRIMARY_DNS, "www.example.org")
        check("out-of-zone REFUSED", r.header.rcode == RCODE.REFUSED)
        r = dns_query(PRIMARY_DNS, f"db.{ZONE}", qtype="AAAA")
        check("AAAA query -> NOERROR NODATA", r.header.rcode == RCODE.NOERROR
              and len(r.rr) == 0)

        print("[4] backup manual sync then answers the same version")
        s = httpx.post(f"{BACKUP_API}/api/sync", timeout=8)
        check("manual sync applied serial 1",
              s.status_code == 200
              and s.json()["status"] == "synced"
              and s.json()["local_serial"] == 1, s.text)
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("backup UDP serves synced record",
              [str(x.rdata) for x in r.rr] == ["10.0.0.11"])
        check("backup zone payload equals primary",
              httpx.get(f"{BACKUP_API}/api/zone").json()
              == httpx.get(f"{PRIMARY_API}/api/zone").json())

        print("[5] slow/broken transfer: backup keeps serving the old version")
        publish(PRIMARY_API, [
            {"name": f"db.{ZONE}", "ip": "10.0.0.12"},
            {"name": f"web.{ZONE}", "ip": "10.0.0.22"},
        ])
        check("primary advanced to serial 2",
              httpx.get(f"{PRIMARY_API}/api/version").json()["serial"] == 2)
        # Repoint the backup at a blackhole by restarting only the backup.
        stop(backup)
        backup = start_service(
            "backup", 18001, 15054, str(DATA / "backup-e2e.db"), BLACKHOLE
        )
        wait_http(f"{BACKUP_API}/health")
        time.sleep(float(SYNC_INTERVAL) + 2.0)
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("backup still answers old v1 during failed transfers",
              [str(x.rdata) for x in r.rr] == ["10.0.0.11"], str(r))
        info = httpx.get(f"{BACKUP_API}/api/version").json()
        check("sync state records failure but serial stays 1",
              info["serial"] == 1
              and info["last_sync_status"] == "failed"
              and bool(info["last_sync_error"]), json.dumps(info))

        print("[6] reconnect -> automatic catch up to the newest version")
        stop(backup)
        backup = start_service(
            "backup", 18001, 15054, str(DATA / "backup-e2e.db"), PRIMARY_API
        )
        wait_http(f"{BACKUP_API}/health")
        deadline = time.time() + 15
        caught = False
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 2:
                caught = True
                break
            time.sleep(0.5)
        check("backup auto-catches-up to serial 2", caught)
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("backup UDP now answers v2",
              [str(x.rdata) for x in r.rr] == ["10.0.0.12"], str(r))
        r = dns_query(BACKUP_DNS, f"web.{ZONE}")
        check("backup v2 second record",
              [str(x.rdata) for x in r.rr] == ["10.0.0.22"])

        print("[7] stale/old transfer can never roll back the active version")
        # Simulate a late old transfer directly against the backup's DB.
        sqlite_env = os.environ.copy()
        sqlite_env["DB_PATH"] = str(DATA / "backup-e2e.db")
        sqlite_env["PYTHONPATH"] = str(ROOT)
        stale = subprocess.run(
            [PY, "-I", str(ROOT / "tests" / "inject_stale.py")],
            cwd=str(ROOT), env=sqlite_env, capture_output=True, text=True,
        )
        check("commit_version(1) rejected -> False",
              stale.stdout.strip().endswith("False"),
              stale.stdout + stale.stderr)
        info = httpx.get(f"{BACKUP_API}/api/version").json()
        check("active serial still 2, no rollback", info["serial"] == 2)
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("DNS still serves v2 IP",
              [str(x.rdata) for x in r.rr] == ["10.0.0.12"])

        print("[8] restart backup: persisted DB + startup catch-up (serial 3)")
        publish(PRIMARY_API, [
            {"name": f"db.{ZONE}", "ip": "10.0.0.13"},
        ])
        check("primary advanced to serial 3",
              httpx.get(f"{PRIMARY_API}/api/version").json()["serial"] == 3)
        stop(backup)
        # While the backup is down its UDP port must be closed.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        down = False
        try:
            sock.sendto(DNSRecord.question(f"db.{ZONE}").pack(), BACKUP_DNS)
            sock.recvfrom(4096)
        except OSError:
            down = True
        finally:
            sock.close()
        check("backup actually stopped", down)
        backup = start_service(
            "backup", 18001, 15054, str(DATA / "backup-e2e.db"), PRIMARY_API
        )
        wait_http(f"{BACKUP_API}/health")
        deadline = time.time() + 15
        caught = False
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 3:
                caught = True
                break
            time.sleep(0.5)
        check("after restart backup reaches serial 3", caught)
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("after restart backup answers v3",
              [str(x.rdata) for x in r.rr] == ["10.0.0.13"], str(r))

        print("[9] primary never waits for backup: publish with backup down")
        stop(backup)
        t0 = time.time()
        resp = publish(PRIMARY_API, [
            {"name": f"db.{ZONE}", "ip": "10.0.0.14"}
        ])
        elapsed = time.time() - t0
        check("publish succeeds immediately without backup",
              resp.status_code == 201 and resp.json()["serial"] == 4
              and elapsed < 3.0, f"{elapsed:.2f}s")
    finally:
        stop(primary)
        if backup.poll() is None:
            stop(backup)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
