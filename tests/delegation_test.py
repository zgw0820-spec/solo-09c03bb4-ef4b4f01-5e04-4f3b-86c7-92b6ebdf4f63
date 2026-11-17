"""End-to-end delegation test: publish, referral, conflict, sync, switch.

Run with the project venv:  .venv/bin/python -I tests/delegation_test.py

Scenarios:
 1. publish a zone with a child delegation -> serial 1
 2. parent queries still answer normally; child/below-child queries get a
    non-authoritative referral with NS in authority and glue in additional
 3. a conflicting submission (parent A inside the child) is rejected and
    changes neither serial nor the active boundary
 4. backup syncs the version and serves the same referral (no stale A, no
    NXDOMAIN)
 5. a second delegation version is synced; the new boundary replaces the
    old one, and no answer mixes NS/glue from both versions
 6. while a slow transfer is in flight the backup keeps the OLD boundary;
    only after the full payload does it switch to the new one
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from dnslib import DNSRecord, QTYPE, RCODE

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

ZONE = "internal.local"
PRIMARY_API = "http://127.0.0.1:18020"
BACKUP_API = "http://127.0.0.1:18021"
PRIMARY_DNS = ("127.0.0.1", 15060)
BACKUP_DNS = ("127.0.0.1", 15061)
SLOW_API = "http://127.0.0.1:18098"
SYNC_INTERVAL = "2"
DELAY = 4.0

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
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"service not ready: {url}")


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
            "DNS_ROLE": role, "DNS_ZONE": ZONE, "DB_PATH": db,
            "DNS_HOST": "127.0.0.1", "DNS_PORT": str(dns_port),
            "API_HOST": "127.0.0.1", "API_PORT": str(api_port),
            "SYNC_INTERVAL": SYNC_INTERVAL, "HTTP_TIMEOUT": "10",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if primary_url:
        env["PRIMARY_API_URL"] = primary_url
    log = open(DATA / f"{role}-deleg.log", "w")
    return subprocess.Popen(
        [PY, "-I", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def stop(proc: subprocess.Popen) -> None:
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def publish(api: str, records, delegation=None) -> httpx.Response:
    body = {"records": records}
    if delegation is not None:
        body["delegation"] = delegation
    return httpx.post(f"{api}/api/zone", json=body, timeout=5)


def delegation(name, ns, glue):
    return {"name": name, "ns": ns, "glue": glue}


def referral_shape(reply):
    return (
        reply.header.rcode,
        reply.header.aa,
        [str(rr.rname) for rr in reply.auth],
        [str(rr.rdata) for rr in reply.auth],
        [(str(rr.rname), str(rr.rdata)) for rr in reply.ar],
        [str(rr.rdata) for rr in reply.rr],
    )


def is_referral(reply, child, ns, glue):
    return referral_shape(reply) == (
        RCODE.NOERROR, 0,
        [child + "."], [ns + "."], [(ns + ".", glue)], [],
    )


class SlowHandler(BaseHTTPRequestHandler):
    """Serves the serial-3 payload (new delegation) after a delay."""

    def do_GET(self):
        if self.path != "/api/zone":
            self.send_response(404)
            self.end_headers()
            return
        time.sleep(DELAY)
        body = json.dumps({
            "zone": ZONE, "serial": 3,
            "records": [{"name": f"db.{ZONE}", "ip": "10.0.0.31"}],
            "delegation": delegation("stage.internal.local",
                                     "ns.stage.internal.local", "10.0.0.83"),
            "published_at": "2026-11-17T00:00:00Z",
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> int:
    for f in ("primary-deleg.db", "backup-deleg.db"):
        for suffix in ("", "-wal", "-shm"):
            p = DATA / (f + suffix)
            if p.exists():
                p.unlink()

    dev = delegation("dev.internal.local", "ns1.dev.internal.local",
                     "10.0.0.53")
    ops = delegation("ops.internal.local", "ns.ops.internal.local",
                     "10.0.0.63")

    primary = start_service(
        "primary", 18020, 15060, str(DATA / "primary-deleg.db"), None
    )
    backup = start_service(
        "backup", 18021, 15061, str(DATA / "backup-deleg.db"), PRIMARY_API
    )
    slow_srv = ThreadingHTTPServer(("127.0.0.1", 18098), SlowHandler)
    threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
    try:
        wait_http(f"{PRIMARY_API}/health")
        wait_http(f"{BACKUP_API}/health")

        print("[1] publish zone + delegation (serial 1)")
        spec = httpx.get(f"{PRIMARY_API}/openapi.json", timeout=3).json()
        check("Swagger documents delegation input",
              set(spec["components"]["schemas"]["DelegationInput"]
                  ["required"]) == {"name", "ns", "glue"}
              and "delegation" in spec["components"]["schemas"]
              ["ZonePayload"]["properties"])
        check("Swagger UI reachable",
              httpx.get(f"{PRIMARY_API}/docs", timeout=3).status_code == 200)
        resp = publish(
            PRIMARY_API,
            [{"name": f"db.{ZONE}", "ip": "10.0.0.11"},
             {"name": f"web.{ZONE}", "ip": "10.0.0.21"}],
            dev,
        )
        check("publish with delegation -> 201 serial 1",
              resp.status_code == 201 and resp.json()["serial"] == 1,
              resp.text)
        check("publish response echoes delegation",
              resp.json().get("delegation") == dev, resp.text)
        zone = httpx.get(f"{PRIMARY_API}/api/zone").json()
        check("GET /api/zone carries delegation",
              zone["delegation"] == dev, json.dumps(zone))

        print("[2] primary: parent answers normal, child gets referral")
        r = dns_query(PRIMARY_DNS, f"db.{ZONE}")
        check("parent A record answered",
              [str(x.rdata) for x in r.rr] == ["10.0.0.11"], str(r))
        r = dns_query(PRIMARY_DNS, f"missing.{ZONE}")
        check("other parent name still NXDOMAIN",
              r.header.rcode == RCODE.NXDOMAIN)
        for name in ("dev.internal.local", "www.dev.internal.local",
                     "a.b.dev.internal.local"):
            r = dns_query(PRIMARY_DNS, name)
            check(f"referral for {name} (A)",
                  is_referral(r, "dev.internal.local",
                              "ns1.dev.internal.local", "10.0.0.53"),
                  str(r))
            r = dns_query(PRIMARY_DNS, name, qtype="AAAA")
            check(f"referral for {name} (AAAA, not NODATA)",
                  is_referral(r, "dev.internal.local",
                              "ns1.dev.internal.local", "10.0.0.53"),
                  str(r))
        r = dns_query(PRIMARY_DNS, f"dev.{ZONE}", qtype="NS")
        check("referral for NS at child apex",
              is_referral(r, "dev.internal.local",
                          "ns1.dev.internal.local", "10.0.0.53"), str(r))

        print("[3] conflicting submission changes nothing")
        conflicts = [
            [{"name": f"db.{ZONE}", "ip": "10.0.0.11"},
             {"name": "www.dev.internal.local", "ip": "10.0.0.99"}],
            [{"name": "dev.internal.local", "ip": "10.0.0.99"}],
        ]
        for records in conflicts:
            bad = publish(PRIMARY_API, records, dev)
            check(f"conflict rejected (422): {records[-1]['name']}",
                  bad.status_code == 422, bad.text)
        bad = publish(PRIMARY_API, [{"name": f"db.{ZONE}", "ip": "10.0.0.11"}],
                      delegation("dev.other.example", "ns1.dev.other.example",
                                 "10.0.0.53"))
        check("delegation outside zone rejected", bad.status_code == 422,
              bad.text)
        bad = publish(PRIMARY_API, [{"name": f"db.{ZONE}", "ip": "10.0.0.11"}],
                      delegation("dev.internal.local", "ns1.other.example",
                                 "10.0.0.53"))
        check("out-of-bailiwick NS rejected", bad.status_code == 422,
              bad.text)
        check("serial unchanged after conflicts",
              httpx.get(f"{PRIMARY_API}/api/version").json()["serial"] == 1)
        check("active delegation unchanged after conflicts",
              httpx.get(f"{PRIMARY_API}/api/zone").json()["delegation"] == dev)

        print("[4] backup syncs the delegation and answers referrals")
        deadline = time.time() + 12
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 1:
                break
            time.sleep(0.3)
        check("backup auto-synced serial 1",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 1)
        check("backup /api/zone equals primary",
              httpx.get(f"{BACKUP_API}/api/zone").json()
              == httpx.get(f"{PRIMARY_API}/api/zone").json())
        r = dns_query(BACKUP_DNS, "www.dev.internal.local")
        check("backup answers referral with NS+glue",
              is_referral(r, "dev.internal.local",
                          "ns1.dev.internal.local", "10.0.0.53"), str(r))
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("backup still answers parent A", [str(x.rdata) for x in r.rr]
              == ["10.0.0.11"], str(r))
        r = dns_query(BACKUP_DNS, f"missing.{ZONE}")
        check("backup unknown parent name NXDOMAIN",
              r.header.rcode == RCODE.NXDOMAIN)

        print("[5] new delegation version replaces old boundary")
        resp = publish(PRIMARY_API, [{"name": f"db.{ZONE}", "ip": "10.0.0.12"}],
                       ops)
        check("publish v2 with new delegation",
              resp.status_code == 201 and resp.json()["serial"] == 2,
              resp.text)
        deadline = time.time() + 12
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 2:
                break
            time.sleep(0.3)
        check("backup synced serial 2",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 2)
        for srv, label in ((PRIMARY_DNS, "primary"), (BACKUP_DNS, "backup")):
            r = dns_query(srv, "x.ops.internal.local")
            check(f"{label} refers ops child only",
                  is_referral(r, "ops.internal.local",
                              "ns.ops.internal.local", "10.0.0.63"), str(r))
            # The previous child is no longer delegated: authoritative NXDOMAIN.
            r = dns_query(srv, "x.dev.internal.local")
            check(f"{label} old child no longer referred",
                  r.header.rcode == RCODE.NXDOMAIN and not r.auth and not r.ar,
                  str(r))
            r = dns_query(srv, f"db.{ZONE}")
            check(f"{label} serves v2 parent A",
                  [str(x.rdata) for x in r.rr] == ["10.0.0.12"], str(r))

        print("[6] slow transfer: old boundary until the payload completes")
        stop(backup)
        t0 = time.time()
        backup = start_service(
            "backup", 18021, 15061, str(DATA / "backup-deleg.db"), SLOW_API
        )
        wait_http(f"{BACKUP_API}/health")
        old_ok, probes = 0, 0
        while time.time() - t0 < DELAY - 0.8:
            try:
                r = dns_query(BACKUP_DNS, "x.ops.internal.local")
                probes += 1
                if is_referral(r, "ops.internal.local",
                               "ns.ops.internal.local", "10.0.0.63"):
                    old_ok += 1
            except Exception:
                pass
            time.sleep(0.4)
        check(f"UDP kept OLD delegation in flight ({probes} probes)",
              probes >= 3 and old_ok == probes,
              f"probes={probes} old={old_ok}")
        check("serial still 2 while transfer incomplete",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 2)
        deadline = time.time() + 12
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 3:
                break
            time.sleep(0.3)
        check("backup applied serial 3 after slow transfer",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 3)
        r = dns_query(BACKUP_DNS, "x.stage.internal.local")
        check("UDP now serves NEW delegation (single version)",
              is_referral(r, "stage.internal.local",
                          "ns.stage.internal.local", "10.0.0.83"), str(r))
        r = dns_query(BACKUP_DNS, "x.ops.internal.local")
        check("old delegation gone after the switch",
              r.header.rcode == RCODE.NXDOMAIN and not r.auth, str(r))
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("UDP serves serial-3 parent A",
              [str(x.rdata) for x in r.rr] == ["10.0.0.31"], str(r))

        print("[7] broken transfer keeps the previous complete boundary")
        resp = publish(PRIMARY_API, [{"name": f"db.{ZONE}", "ip": "10.0.0.14"}],
                       delegation("test.internal.local",
                                  "ns1.test.internal.local", "10.0.0.73"))
        check("primary advanced to serial 3 with test delegation",
              resp.status_code == 201 and resp.json()["serial"] == 3,
              resp.text)
        stop(backup)
        backup = start_service(
            "backup", 18021, 15061, str(DATA / "backup-deleg.db"),
            "http://127.0.0.1:19999",
        )
        wait_http(f"{BACKUP_API}/health")
        time.sleep(float(SYNC_INTERVAL) + 2.0)
        info = httpx.get(f"{BACKUP_API}/api/version").json()
        check("failed sync leaves serial at 3 and reports failure",
              info["serial"] == 3 and info["last_sync_status"] == "failed"
              and bool(info["last_sync_error"]), json.dumps(info))
        check("old delegation boundary still served",
              httpx.get(f"{BACKUP_API}/api/zone").json()["delegation"]
              == delegation("stage.internal.local",
                            "ns.stage.internal.local", "10.0.0.83"))
        r = dns_query(BACKUP_DNS, "x.test.internal.local")
        check("no partial new delegation from failed transfer",
              r.header.rcode == RCODE.NXDOMAIN and not r.auth, str(r))
        r = dns_query(BACKUP_DNS, f"db.{ZONE}")
        check("old parent A still served during failure",
              [str(x.rdata) for x in r.rr] == ["10.0.0.31"], str(r))
    finally:
        slow_srv.shutdown()
        stop(primary)
        if backup.poll() is None:
            stop(backup)

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
