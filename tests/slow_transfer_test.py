"""Literal slow-transfer test.

A fake upstream holds the zone response open for ~5s. While that transfer
is still in flight the backup must keep answering the OLD version over
UDP; only after the full payload arrives may it switch to the new one.
"""
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
from dnslib import DNSRecord

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
DATA = ROOT / "data"
PRIMARY_API, SLOW_API, BACKUP_API = (
    "http://127.0.0.1:18010", "http://127.0.0.1:18099",
    "http://127.0.0.1:18011",
)
BACKUP_DNS = ("127.0.0.1", 15057)
failures = []
DELAY = 5.0


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (" " + detail if not cond else ""))
    if not cond:
        failures.append(name)


def wait_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(url)


def start(role, api_port, dns_port, db, upstream):
    env = os.environ.copy()
    env.update({
        "DNS_ROLE": role, "DNS_ZONE": "internal.local", "DB_PATH": db,
        "DNS_HOST": "127.0.0.1", "DNS_PORT": str(dns_port),
        "API_HOST": "127.0.0.1", "API_PORT": str(api_port),
        "SYNC_INTERVAL": "60", "HTTP_TIMEOUT": "15", "PYTHONUNBUFFERED": "1",
    })
    if upstream:
        env["PRIMARY_API_URL"] = upstream
    log = open(DATA / f"{role}-slow.log", "w")
    return subprocess.Popen(
        [PY, "-I", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)


def stop(p):
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)


def dns_ip():
    q = DNSRecord.question("db.internal.local")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(q.pack(), BACKUP_DNS)
    reply = DNSRecord.parse(s.recvfrom(65535)[0])
    s.close()
    return [str(rr.rdata) for rr in reply.rr]


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/zone":
            self.send_response(404)
            self.end_headers()
            return
        time.sleep(DELAY)  # slow, in-flight transfer
        body = json.dumps({
            "zone": "internal.local", "serial": 2,
            "records": [{"name": "db.internal.local", "ip": "10.0.0.12"}],
            "published_at": "2026-11-16T00:00:00Z",
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    for f in ("primary-slow.db", "backup-slow.db"):
        for s in ("", "-wal", "-shm"):
            p = DATA / (f + s)
            if p.exists():
                p.unlink()

    primary = start("primary", 18010, 15058,
                    str(DATA / "primary-slow.db"), None)
    slow_srv = ThreadingHTTPServer(("127.0.0.1", 18099), SlowHandler)
    threading.Thread(target=slow_srv.serve_forever, daemon=True).start()
    backup = None
    try:
        wait_http(f"{PRIMARY_API}/health")
        httpx.post(f"{PRIMARY_API}/api/zone", json={"records": [
            {"name": "db.internal.local", "ip": "10.0.0.11"}]}, timeout=5)

        # Seed the backup at serial 1 via the real primary.
        backup = start("backup", 18011, 15057,
                       str(DATA / "backup-slow.db"), PRIMARY_API)
        wait_http(f"{BACKUP_API}/health")
        deadline = time.time() + 12
        while time.time() < deadline and \
                httpx.get(f"{BACKUP_API}/api/version").json()["serial"] < 1:
            time.sleep(0.3)
        check("backup seeded at serial 1",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 1)
        stop(backup)

        # Restart against the slow upstream; the only sync is the startup one.
        t0 = time.time()
        backup = start("backup", 18011, 15057,
                       str(DATA / "backup-slow.db"), SLOW_API)
        wait_http(f"{BACKUP_API}/health")

        print(f"[slow transfer ~{DELAY}s] probing UDP while transfer in flight")
        answered_old, probes = 0, 0
        while time.time() - t0 < DELAY - 0.5:
            try:
                ips = dns_ip()
                probes += 1
                if ips == ["10.0.0.11"]:
                    answered_old += 1
            except Exception:
                pass
            time.sleep(0.5)
        check(f"UDP kept answering OLD version in flight ({probes} probes)",
              probes >= 3 and answered_old == probes,
              f"probes={probes} old_answers={answered_old}")
        check("serial still 1 while transfer incomplete",
              httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 1)

        print("[after full payload] atomic switch to the new version")
        deadline = time.time() + 12
        ok = False
        while time.time() < deadline:
            if httpx.get(f"{BACKUP_API}/api/version").json()["serial"] == 2:
                ok = True
                break
            time.sleep(0.3)
        check("backup applied serial 2 after slow transfer completed", ok)
        check("UDP now answers new IP", dns_ip() == ["10.0.0.12"])
    finally:
        slow_srv.shutdown()
        stop(primary)
        if backup and backup.poll() is None:
            stop(backup)
    print(f"\n==== slow-transfer checks: {len(failures)} failures ====")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
