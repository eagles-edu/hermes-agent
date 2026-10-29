"""Small compatibility helpers for the session spawn-tree RPCs."""

import contextlib
import json


def _legacy_spawn_tree_entry(path, session_dir_name: str) -> dict | None:
    """Index-shaped entry for a pre-index snapshot file (None when unreadable)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    raw = {}
    with contextlib.suppress(Exception):
        raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    subagents = raw.get("subagents") or []
    return {"path": str(path), "session_id": raw.get("session_id") or session_dir_name,
            "finished_at": raw.get("finished_at") or stat.st_mtime, "started_at": raw.get("started_at"),
            "label": raw.get("label") or "", "count": len(subagents) if isinstance(subagents, list) else 0}
