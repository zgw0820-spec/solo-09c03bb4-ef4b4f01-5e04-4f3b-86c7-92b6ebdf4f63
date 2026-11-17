"""Focused checks: background auto-sync (no manual trigger) and idempotency."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dnslib import DNSRecord

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
DATA = ROOT / "data"
PRIMARY_API, BACKUP_API = "http://127.0.0.1:18002", "http://127.0.0.1:18003"
BACKUP_DNS = ("127.0.0.1", 15055)
failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (" " + detail if not cond else ""))
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
        "SYNC_INTERVAL": "1", "HTTP_TIMEOUT": "3", "PYTHONUNBUFFERED": "1",
    })
    if upstream:
        env["PRIMARY_API_URL"] = upstream
    log = open(DATA / f"{role}-auto.log", "w")
    return subprocess.Popen(
        [PY, "-I", "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)


def stop(p):
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)


def serial(api):
    return httpx.get(f"{api}/api/version", timeout=3).json()["serial"]


def main():
    for f in ("primary-auto.db", "backup-auto.db"):
        for s in ("", "-wal", "-shm"):
            p = DATA / (f + s)
            if p.exists():
                p.unlink()
    primary = start("primary", 18002, 15056, str(DATA / "primary-auto.db"), None)
    backup = start("backup", 18003, 15055, str(DATA / "backup-auto.db"), PRIMARY_API)
    try:
        wait_http(f"{PRIMARY_API}/health")
        wait_http(f"{BACKUP_API}/health")

        print("[A] auto-sync: never call /api/sync manually")
        httpx.post(f"{PRIMARY_API}/api/zone", json={"records": [
            {"name": "svc.internal.local", "ip": "10.1.0.1"}]}, timeout=5)
        deadline = time.time() + 12
        while time.time() < deadline and serial(BACKUP_API) < 1:
            time.sleep(0.3)
        check("backup reached serial 1 via background loop only",
              serial(BACKUP_API) == 1)
        httpx.post(f"{PRIMARY_API}/api/zone", json={"records": [
            {"name": "svc.internal.local", "ip": "10.1.0.2"}]}, timeout=5)
        deadline = time.time() + 12
        while time.time() < deadline and serial(BACKUP_API) < 2:
            time.sleep(0.3)
        check("backup auto-followed to serial 2", serial(BACKUP_API) == 2)

        print("[B] repeated sync at same serial is idempotent (no rollback)")
        s1 = httpx.post(f"{BACKUP_API}/api/sync", timeout=6).json()
        s2 = httpx.post(f"{BACKUP_API}/api/sync", timeout=6).json()
        check("first manual sync marks stale/ignored",
              s1["status"] == "ignored_stale", str(s1))
        check("second manual sync identical outcome, serial unchanged",
              s2["status"] == "ignored_stale" and s2["local_serial"] == 2)
        q = DNSRecord.question("svc.internal.local")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2)
        sock.sendto(q.pack(), BACKUP_DNS)
        reply = DNSRecord.parse(sock.recvfrom(65535)[0])
        sock.close()
        check("DNS still serves serial-2 IP",
              [str(rr.rdata) for rr in reply.rr] == ["10.1.0.2"])

        print("[C] Swagger / OpenAPI is exposed")
        spec = httpx.get(f"{PRIMARY_API}/openapi.json", timeout=3).json()
        paths = sorted(spec["paths"].keys())
        check("openapi documents all endpoints",
              paths == ["/api/sync", "/api/version", "/api/zone", "/health"],
              str(paths))
        ui = httpx.get(f"{PRIMARY_API}/docs", timeout=3)
        check("Swagger UI reachable", ui.status_code == 200)
    finally:
        stop(primary)
        if backup.poll() is None:
            stop(backup)
    print(f"\n==== auto-sync checks: {len(failures)} failures ====")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
