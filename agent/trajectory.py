"""Trajectory saving + scratchpad helpers (``_convert_to_trajectory_format`` stays an AIAgent method — batch_runner.py calls it)."""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List
from agent.tool_argument_log_safety import (
    is_curator_v2_tool,
    tool_arguments_log_excerpt,
)

logger = logging.getLogger(__name__)


def _trajectory_gpt_prefix(msg: dict[str, Any]) -> str:
    """Leading ``<think>`` block from native reasoning tokens, if any."""
    if msg.get("reasoning") and msg["reasoning"].strip():
        return f"<think>\n{msg['reasoning']}\n</think>\n"
    return ""


def _with_think_block(content: str) -> str:
    """Every gpt turn gets a <think> block (empty if none) for a consistent training format."""
    return content if "<think>" in content else "<think>\n</think>\n" + content


def _trajectory_tool_call_turn(msg: dict[str, Any]) -> str:
    content = _trajectory_gpt_prefix(msg)
    if msg.get("content") and msg["content"].strip():
        # <REASONING_SCRATCHPAD> -> <think> (model reasons via XML when native thinking is off)
        content += convert_scratchpad_to_think(msg["content"]) + "\n"
    for tool_call in msg["tool_calls"]:
        if not tool_call or not isinstance(tool_call, dict):
            continue
        function = tool_call["function"]
        tool_name = function["name"]
        raw_args = function["arguments"]
        if is_curator_v2_tool(tool_name):
            # Trajectory samples are durable local exports; preserve the call
            # boundary while excluding its potentially sensitive payload.
            arguments = {"_redacted": True}
        else:
            # Arguments were validated during conversation; degrade to {} rather than abort.
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                logger.warning(
                    "Unexpected invalid JSON in trajectory conversion: %s",
                    tool_arguments_log_excerpt(tool_name, raw_args, 100),
                )
                arguments = {}
        tool_call_json = {"name": tool_name, "arguments": arguments}
        content += f"<tool_call>\n{json.dumps(tool_call_json, ensure_ascii=False)}\n</tool_call>\n"
    return _with_think_block(content).rstrip()


def _trajectory_tool_responses(
    msg: dict[str, Any], messages: list[dict[str, Any]], start: int
) -> tuple[list[str], int]:
    """Collect paired tool responses, redacting Curator V2 payloads entirely."""
    tool_responses = []
    index = start
    while index < len(messages) and messages[index]["role"] == "tool":
        tool_msg = messages[index]
        tool_index = len(tool_responses)
        tool_name = (
            msg["tool_calls"][tool_index]["function"]["name"]
            if tool_index < len(msg["tool_calls"])
            else "unknown"
        )
        tool_content = tool_msg["content"]
        if is_curator_v2_tool(tool_name):
            tool_content = {"_redacted": True}
        else:
            try:  # Pretty-print non-Curator tool content when it looks like JSON.
                if tool_content.strip().startswith(("{", "[")):
                    tool_content = json.loads(tool_content)
            except (json.JSONDecodeError, AttributeError):
                pass
        payload = json.dumps(
            {
                "tool_call_id": tool_msg.get("tool_call_id", ""),
                "name": tool_name,
                "content": tool_content,
            },
            ensure_ascii=False,
        )
        tool_responses.append(f"<tool_response>\n{payload}\n</tool_response>")
        index += 1
    return tool_responses, index


def convert_scratchpad_to_think(content: str) -> str:
    """Convert <REASONING_SCRATCHPAD> tags to <think> tags."""
    if not content or "<REASONING_SCRATCHPAD>" not in content:
        return content
    return content.replace("<REASONING_SCRATCHPAD>", "<think>").replace("</REASONING_SCRATCHPAD>", "</think>")


def has_incomplete_scratchpad(content: str) -> bool:
    """Whether content has an opening <REASONING_SCRATCHPAD> without a closing tag."""
    return bool(content) and "<REASONING_SCRATCHPAD>" in content and "</REASONING_SCRATCHPAD>" not in content


def _lock_append_handle(f, acquire: bool) -> None:
    """Exclusive whole-file lock on an append handle: ``flock`` on POSIX, a 1-byte
    ``msvcrt.locking`` range at offset 0 on Windows (append position is restored by the OS)."""
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
        f.seek(0, os.SEEK_END)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


def save_trajectory(trajectory: list[dict[str, Any]], model: str, completed: bool, filename: str | None = None):
    """Append a ShareGPT-format entry to a JSONL file (default trajectory_samples.jsonl / failed_trajectories.jsonl by ``completed``)."""
    if filename is None:
        filename = "trajectory_samples.jsonl" if completed else "failed_trajectories.jsonl"
    entry = {"conversations": trajectory, "timestamp": datetime.now().isoformat(), "model": model, "completed": completed}
    try:
        line = json.dumps(entry, ensure_ascii=False) + "\n"  # serialize before taking the lock
        with open(filename, "a", encoding="utf-8") as f:
            # Gateway sessions and batch workers append to the SAME default file; without an
            # exclusive lock around write+flush, entries larger than one write() interleave and the
            # JSONL stops parsing (#12684).
            _lock_append_handle(f, True)
            try:
                f.write(line)
                f.flush()
            finally:
                _lock_append_handle(f, False)
        logger.info("Trajectory saved to %s", filename)
    except Exception as e:
        logger.warning("Failed to save trajectory: %s", e)
