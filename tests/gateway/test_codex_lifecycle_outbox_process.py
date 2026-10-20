"""Exercise Codex lifecycle outbox recovery across real process exits."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


_REPOSITORY = Path(__file__).resolve().parents[2]
_PEER = r'''
import json
import os
import sys

from gateway.codex_outbox import CodexLifecycleOutbox

path = sys.argv[1]
mode = sys.argv[2]
expected = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
outbox = CodexLifecycleOutbox(path, profile_id="process-recovery-test")
context = {"attempt_id": "attempt-1", "session_id": "session-1"}

if mode == "seed":
    assert outbox.bind_goal("project-1", "session-1", "goal-1")
    event = outbox.enqueue("codex:turn:start", context)
    assert event is not None
    assert event["source_sequence"] == 1
    sys.stdout.write(json.dumps(event, sort_keys=True) + "\n")
    sys.stdout.flush()
    os._exit(0)

assert expected is not None
if mode == "verify-ack":
    assert outbox.pending() == []
    assert outbox.enqueue("codex:turn:start", context) == expected
    assert outbox.pending() == []
    sys.stdout.write("acknowledged\n")
    sys.stdout.flush()
    outbox.close()
    raise SystemExit(0)

assert outbox.pending() == [expected]
assert outbox.enqueue("codex:turn:start", context) == expected
if mode in {"retry", "verify-retry"}:
    if mode == "retry":
        outbox.mark_attempt(expected["event_id"], acknowledged=False)
    else:
        sys.stdout.write(json.dumps(outbox.pending()[0], sort_keys=True) + "\n")
        sys.stdout.flush()
        os._exit(0)
elif mode == "acknowledge":
    outbox.mark_attempt(expected["event_id"], acknowledged=True)
else:
    raise AssertionError("unknown process-recovery mode")

# Model abrupt worker termination after the SQLite operation committed.
os._exit(0)
'''


def _run_peer(
    database: Path, mode: str, expected: dict[str, object] | None = None
) -> str:
    arguments = [sys.executable, "-c", _PEER, str(database), mode]
    if expected is not None:
        arguments.append(json.dumps(expected, sort_keys=True))
    result = subprocess.run(
        arguments,
        cwd=_REPOSITORY,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_durable_outbox_recovers_exact_event_across_process_crashes(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    event = json.loads(_run_peer(database, "seed"))

    # A failed delivery attempt leaves the same sequence and envelope pending.
    _run_peer(database, "retry", event)
    replay = json.loads(_run_peer(database, "verify-retry", event))
    assert replay == event

    # A second fresh process acknowledges the event; recovery no longer replays it.
    _run_peer(database, "acknowledge", event)
    assert _run_peer(database, "verify-ack", event) == "acknowledged\n"
