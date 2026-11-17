"""Helper for the e2e stale-transfer check (run with python -I)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import storage  # noqa: E402

# A late, older complete version (serial 1) arriving while serial 2 is
# active must be rejected and must never roll the zone back.
print(
    storage.commit_version(
        1,
        [{"name": "db.internal.local", "ip": "10.9.9.9"}],
        "late-old-transfer",
    )
)
