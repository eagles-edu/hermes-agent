"""Curator V2 payloads must not enter gateway progress logs or previews."""

import queue
from types import SimpleNamespace

from gateway.run_turn_progress import GatewayTurnProgressMixin


def test_curator_v2_progress_preview_is_redacted_before_log_and_status_projection():
    class ProgressProbe(GatewayTurnProgressMixin):
        def __init__(self):
            self._approval_owner = None
            self.log_queue = queue.Queue()
            self.status_args = []
            self.progress_args = []
            self._ctx = SimpleNamespace(
                log_queue=self.log_queue,
                progress_queue=queue.Queue(),
                _run_still_current=lambda: True,
                long_tool_hint_fired=[False],
                _native_slack_task_cards=False,
                tool_progress_enabled=True,
                progress_mode="verbose",
                last_tool=[None],
            )

        def _progress_live_status(self, _event_type, _tool_name, args):
            self.status_args.append(args)

        def _progress_build_message(self, tool_name, preview, args):
            self.progress_args.append((tool_name, preview, args))

    probe = ProgressProbe()
    canary = "CURATOR_V2_PROGRESS_LOG_CANARY"
    probe.progress_callback(
        "tool.started",
        "mcp__curator_v2__fact_record",
        f"fact preview {canary}",
        {"bullet": canary},
    )

    logged = probe.log_queue.get_nowait()
    assert "[redacted Curator V2 tool arguments]" in logged
    assert canary not in logged
    assert probe.status_args == [None]
    assert probe.progress_args == [
        ("mcp__curator_v2__fact_record", "[redacted Curator V2 tool arguments]", None)
    ]
