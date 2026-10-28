"""Bounded file and quality-check summary for opted-in Curator V2 events."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from typing import Any


_MAX_FILES = 16
_MAX_CHECKS = 8
_MAX_JSON_BYTES = 4096
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./ @+-]{1,128}$")
_PRIVATE_BASENAMES = {".env", ".env.local", "credentials", "secrets"}
_MAX_OBSERVED_CHANGES = 256
_MAX_OMITTED = 1_000_000


def _repository_fingerprint(root: Path) -> str:
    value = b"curatormd-v2-project-root-v1\0" + os.fsencode(str(root))
    return hashlib.sha256(value).hexdigest()


def _safe_check_label(command: object) -> str | None:
    """Recognize a few fixed quality commands without retaining their arguments."""

    if not isinstance(command, str) or not command or len(command) > 4096:
        return None
    if any(char in command for char in "\n\r;|&<>`$"):
        return None
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if not parts or len(parts) > 32:
        return None
    tokens = tuple(part.replace("\\", "/") for part in parts)
    if tokens == ("git", "diff", "--check"):
        return "git diff --check"
    if tokens in {
        ("scripts/check",),
        ("python", "scripts/check"),
        ("python3", "scripts/check"),
        ("uv", "run", "scripts/check"),
        ("uv", "run", "--locked", "scripts/check"),
    }:
        return "scripts/check"
    if (
        tokens[:1] == ("scripts/run_tests.sh",)
        and all(_is_test_target(token) for token in tokens[1:])
    ):
        return "scripts/run_tests.sh"
    if tokens == ("pytest",) or (
        tokens[:1] == ("pytest",)
        and all(_is_pytest_option_or_target(token) for token in tokens[1:])
    ):
        return "pytest"
    if tokens[:3] in {
        ("python", "-m", "pytest"),
        ("python3", "-m", "pytest"),
    } and all(_is_pytest_option_or_target(token) for token in tokens[3:]):
        return "pytest"
    if tokens == ("npm", "test"):
        return "npm test"
    return None


def _is_test_target(token: str) -> bool:
    """Allow only relative tests/ paths after the test-runner script."""

    path = Path(token)
    return (
        token.startswith("tests/")
        and not path.is_absolute()
        and ".." not in path.parts
        and bool(_SAFE_PATH.fullmatch(token))
    )


def _is_pytest_option_or_target(token: str) -> bool:
    """Permit ordinary test paths and a small set of run-mode flags."""

    if token in {"-q", "-v", "-x", "--maxfail=1"}:
        return True
    return _is_test_target(token)


class CodexWorkSummary:
    """Collect only project-relative file changes and allowlisted command exit codes."""

    def __init__(self, project_root: str | None) -> None:
        try:
            self._root = Path(project_root).resolve(strict=True) if project_root else None
        except OSError:
            self._root = None
        self._files: dict[str, str] = {}
        self._checks: list[dict[str, Any]] = []
        self._omitted_files = 0
        self._omitted_checks = 0

    def observe(self, item: object) -> None:
        """Consume one completed app-server item, dropping all command output."""

        if not isinstance(item, dict):
            return
        if item.get("type") == "fileChange":
            self._observe_changes(item)
        elif item.get("type") == "commandExecution":
            self._observe_check(item)

    def _observe_changes(self, item: dict[str, Any]) -> None:
        if item.get("status") not in {"completed", "applied", "success"}:
            return
        changes = item.get("changes")
        if not isinstance(changes, list):
            return
        for index, change in enumerate(changes):
            if index >= _MAX_OBSERVED_CHANGES:
                self._omitted_files = min(
                    _MAX_OMITTED,
                    self._omitted_files + len(changes) - index,
                )
                break
            if not isinstance(change, dict):
                continue
            relative = self._relative_path(change.get("path"))
            if relative is None:
                self._omitted_files = min(_MAX_OMITTED, self._omitted_files + 1)
                continue
            kind = change.get("kind")
            kind = kind.get("type") if isinstance(kind, dict) else kind
            kind = kind if kind in {"add", "update", "delete", "rename"} else "update"
            if relative in self._files or len(self._files) < _MAX_FILES:
                self._files[relative] = kind
            else:
                self._omitted_files = min(_MAX_OMITTED, self._omitted_files + 1)

    def _relative_path(self, raw_path: object) -> str | None:
        if self._root is None or not isinstance(raw_path, str) or not raw_path:
            return None
        if "\\" in raw_path or "\n" in raw_path or "\r" in raw_path:
            return None
        candidate_path = Path(raw_path)
        if not candidate_path.is_absolute() and ".." in candidate_path.parts:
            return None
        try:
            candidate = (
                candidate_path.resolve(strict=False)
                if candidate_path.is_absolute()
                else (self._root / candidate_path).resolve(strict=False)
            )
            relative = candidate.relative_to(self._root).as_posix()
        except (OSError, ValueError):
            return None
        if (
            relative in {"", "."}
            or not _SAFE_PATH.fullmatch(relative)
            or Path(relative).name.lower() in _PRIVATE_BASENAMES
            or any(part.lower().startswith((".env", "secret", "credential"))
                   for part in Path(relative).parts)
        ):
            return None
        return relative

    def _observe_check(self, item: dict[str, Any]) -> None:
        label = _safe_check_label(item.get("command"))
        exit_code = item.get("exitCode")
        if label is None or type(exit_code) is not int or not 0 <= exit_code <= 255:
            return
        entry = {"name": label, "exit_code": exit_code}
        if len(self._checks) < _MAX_CHECKS:
            self._checks.append(entry)
        else:
            self._omitted_checks = min(_MAX_OMITTED, self._omitted_checks + 1)

    def to_json(self) -> str | None:
        """Return a compact versioned summary, or ``None`` when there is no evidence."""

        if self._root is None or (not self._files and not self._checks and not self._omitted_files and not self._omitted_checks):
            return None
        payload = {
            "schema_version": 1,
            "repository_fingerprint": _repository_fingerprint(self._root),
            "changed_files": [
                {"path": path, "change": change}
                for path, change in sorted(self._files.items())
            ],
            "omitted_files": self._omitted_files,
            "checks": self._checks,
            "omitted_checks": self._omitted_checks,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return encoded if len(encoded.encode("utf-8")) <= _MAX_JSON_BYTES else None
