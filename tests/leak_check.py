"""Ad-hoc guard: SQLite connections must be closed on every code path."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import storage  # noqa: E402

for i in range(1, 51):
    storage.next_publish(
        [{"name": f"h{i}.internal.local", "ip": f"10.0.0.{i}"}]
    )
    # stale rejects, reads, sync-state writes, existence checks
    storage.commit_version(i, [], "stale")
    storage.get_active_version()
    storage.active_serial()
    storage.get_sync_state()
    storage.update_sync_state("synced", None, i)
    storage.version_exists(i)

db_fds = []
for f in os.listdir("/proc/self/fd"):
    try:
        target = os.readlink(f"/proc/self/fd/{f}")
    except OSError:
        continue
    if "leakcheck" in target:
        db_fds.append((f, target))
assert db_fds == [], f"connection leak: {db_fds}"
print("no leaked connections after 350+ storage operations")
