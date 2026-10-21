"""Profile-local token aggregation across Hermes provider/model routes."""

from types import SimpleNamespace


def test_turn_snapshot_groups_provider_usage_and_marks_missing_usage_unknown():
    from agent.turn_usage import _record_profile_usage, profile_usage_snapshot
    from agent.usage_pricing import CanonicalUsage

    agent = SimpleNamespace(
        _current_turn_id="turn-profile-usage", provider="nous",
        model="anthropic/claude-fable-5.1",
    )
    _record_profile_usage(
        agent,
        CanonicalUsage(
            input_tokens=5, output_tokens=7, cache_read_tokens=10,
            cache_write_tokens=5,
        ),
    )
    agent.provider = "openai"
    agent.model = "gpt-5.6"
    _record_profile_usage(agent, CanonicalUsage(input_tokens=10, output_tokens=7))
    _record_profile_usage(agent, None)

    groups = profile_usage_snapshot(agent, "turn-profile-usage")

    assert [(row["provider"], row["model"], row["usage_status"]) for row in groups] == [
        ("nous", "anthropic/claude-fable-5.1", "reported"),
        ("openai", "gpt-5.6", "unknown"),
    ]
    assert groups[0]["total_tokens"] == 27
    assert groups[1]["total_tokens"] is None
    assert profile_usage_snapshot(agent, "another-turn") == []


def test_response_accounting_collects_each_provider_in_the_current_turn(monkeypatch, tmp_path):
    from agent import turn_usage

    class Compressor:
        threshold_tokens = 1000
        _verify_compaction_cleared_threshold = False
        awaiting_real_usage_after_compression = False
        _context_probed = False

        def update_from_response(self, _usage):
            return None

    agent = SimpleNamespace(
        _current_turn_id="turn-api-usage",
        context_compressor=Compressor(),
        session_api_calls=0,
        provider="anthropic",
        model="claude-sonnet",
        api_mode="chat_completions",
        client=None,
        base_url="",
        api_key="",
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0,
        session_cost_status="unknown",
        session_cost_source="none",
        _session_db=None,
        session_id="session-1",
        verbose_logging=False,
        quiet_mode=True,
        log_prefix="",
    )
    cost = SimpleNamespace(amount_usd=None, status="unknown", source="none")
    monkeypatch.setattr(turn_usage, "calibrate_from_usage", lambda *_args: None)
    monkeypatch.setattr(turn_usage, "capture_usage_anchor", lambda *_args: None)
    monkeypatch.setattr(turn_usage, "estimate_usage_cost", lambda *_args, **_kwargs: cost)
    monkeypatch.setattr(
        turn_usage,
        "_loop_mod",
        lambda: SimpleNamespace(_should_rearm_compression_budget=lambda *_args, **_kwargs: False),
    )

    for provider, model, usage in (
        ("anthropic", "claude-sonnet", SimpleNamespace(input_tokens=20, output_tokens=5)),
        ("openai", "gpt-5.6", SimpleNamespace(prompt_tokens=10, completion_tokens=3)),
        ("openai", "gpt-5.6", None),
    ):
        agent.provider, agent.model = provider, model
        turn_usage.record_response_usage(
            agent,
            SimpleNamespace(usage=usage),
            messages=[{"role": "user", "content": "test"}],
            api_call_count=agent.session_api_calls + 1,
            api_duration=0.1,
            compression_attempts=0,
            max_compression_attempts=3,
        )

    snapshot = turn_usage.profile_usage_snapshot(agent, "turn-api-usage")

    assert [(row["provider"], row["model"], row["usage_status"]) for row in snapshot] == [
        ("anthropic", "claude-sonnet", "reported"),
        ("openai", "gpt-5.6", "unknown"),
    ]
    assert snapshot[0]["total_tokens"] == 25
    assert snapshot[1]["total_tokens"] is None

    from agent.turn_finalizer import _attach_profile_usage
    from gateway.codex_outbox import CodexLifecycleOutbox

    profile_home = tmp_path / ".hermes"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    outbox = CodexLifecycleOutbox(profile_id="usage-test")
    assert outbox.bind_goal("project-1", "session-1", "goal-1")
    agent_result = {}
    _attach_profile_usage(agent_result, agent, "turn-api-usage")

    assert agent_result["_profile_usage_groups"] == snapshot
    assert outbox.record_agent_usage(
        "session-1",
        agent_result["_profile_usage_turn_id"],
        agent_result["_profile_usage_groups"],
    ) == 2
    totals = outbox.project_usage_totals("project-1")
    assert (totals["turns"], totals["reported_turns"], totals["unknown_turns"]) == (1, 0, 1)
    assert totals["total_tokens"] == 25
    outbox.close()


def test_failed_provider_attempt_is_unknown_and_attached_for_gateway_persistence(monkeypatch):
    from agent import turn_api_call
    from agent.turn_usage import profile_usage_snapshot
    from hermes_cli import middleware

    agent = SimpleNamespace(
        _current_turn_id="turn-failed-provider",
        provider="anthropic",
        model="claude-sonnet",
        api_mode="chat_completions",
        base_url="https://provider.invalid",
        session_id="session-failed-provider",
        platform="cli",
        client=None,
        _has_pending_redirect=lambda: False,
        _interruptible_api_call=lambda _kwargs: None,
    )
    monkeypatch.setattr(turn_api_call, "_should_stream", lambda _agent: False)

    def invoke_provider(request, executor, **_kwargs):
        return executor(request)

    def fail_provider(*_args, **_kwargs):
        raise RuntimeError("provider connection dropped")

    monkeypatch.setattr(middleware, "run_llm_execution_middleware", invoke_provider)
    from agent import relay_llm

    monkeypatch.setattr(relay_llm, "execute", fail_provider)

    try:
        turn_api_call.perform_api_call(
            agent,
            api_kwargs={},
            _original_api_kwargs={},
            _llm_middleware_trace=[],
            _moa_prepared_request=None,
            _retry=None,
            thinking_spinner=None,
            retry_count=0,
            api_call_count=1,
            api_request_id="request-1",
            effective_task_id=None,
            turn_id="turn-failed-provider",
            interrupted=None,
        )
    except RuntimeError as exc:
        assert str(exc) == "provider connection dropped"
    else:
        raise AssertionError("the fake provider failure should propagate to retry handling")

    groups = profile_usage_snapshot(agent, "turn-failed-provider")
    assert groups == [{
        "provider": "anthropic",
        "model": "claude-sonnet",
        "usage_status": "unknown",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "reasoning_tokens": None,
    }]

    from agent.turn_finalizer import _attach_profile_usage

    result = {}
    _attach_profile_usage(result, agent, "turn-failed-provider")
    assert result["_profile_usage_groups"] == groups


def test_auxiliary_provider_usage_is_grouped_and_persisted_under_its_actual_model(
    monkeypatch, tmp_path,
):
    from agent.aux_accounting import record_aux_usage, reset_accounting_context, set_accounting_context
    from agent.turn_usage import (
        _record_profile_usage,
        profile_aux_usage_callback,
        profile_usage_snapshot,
    )
    from agent.usage_pricing import CanonicalUsage

    class SessionDB:
        def record_auxiliary_usage(self, *_args, **_kwargs):
            return None

    agent = SimpleNamespace(
        _current_turn_id="turn-with-aux",
        provider="anthropic",
        model="claude-sonnet",
    )
    _record_profile_usage(agent, CanonicalUsage(input_tokens=20, output_tokens=5))

    monkeypatch.setattr(
        "agent.runtime_session_store.call_off_event_loop",
        lambda callback, *args, **kwargs: callback(*args, **kwargs),
    )
    from gateway.codex_outbox import CodexLifecycleOutbox

    profile_home = tmp_path / ".hermes"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    outbox = CodexLifecycleOutbox(profile_id="usage-aux-test")
    assert outbox.bind_goal("project-aux", "session-aux", "goal-aux")
    token = set_accounting_context(
        SessionDB(),
        "session-aux",
        on_usage=profile_aux_usage_callback(agent, "turn-with-aux"),
    )
    try:
        record_aux_usage(
            SimpleNamespace(
                model="gemini-flash",
                usage=CanonicalUsage(input_tokens=7, output_tokens=2),
            ),
            "vision",
            provider="google",
        )
    finally:
        reset_accounting_context(token)

    groups = profile_usage_snapshot(agent, "turn-with-aux")
    assert [(row["provider"], row["model"], row["total_tokens"]) for row in groups] == [
        ("anthropic", "claude-sonnet", 25),
        ("google", "gemini-flash", 9),
    ]
    assert outbox.record_agent_usage("session-aux", "turn-with-aux", groups) == 2
    assert outbox.project_usage_totals("project-aux")["total_tokens"] == 34
    outbox.close()


def test_auxiliary_response_without_usage_marks_only_its_route_unknown():
    from agent.aux_accounting import record_aux_usage, reset_accounting_context, set_accounting_context
    from agent.turn_usage import (
        _record_profile_usage,
        profile_aux_usage_callback,
        profile_usage_snapshot,
    )
    from agent.usage_pricing import CanonicalUsage

    agent = SimpleNamespace(
        _current_turn_id="turn-aux-missing",
        provider="anthropic",
        model="claude-sonnet",
    )
    _record_profile_usage(agent, CanonicalUsage(input_tokens=20, output_tokens=5))
    token = set_accounting_context(
        object(),
        "session-aux",
        on_usage=profile_aux_usage_callback(agent, "turn-aux-missing"),
    )
    try:
        record_aux_usage(
            SimpleNamespace(model="gemini-flash", usage=None), "vision", provider="google"
        )
    finally:
        reset_accounting_context(token)

    groups = profile_usage_snapshot(agent, "turn-aux-missing")
    assert [(row["provider"], row["model"], row["usage_status"]) for row in groups] == [
        ("anthropic", "claude-sonnet", "reported"),
        ("google", "gemini-flash", "unknown"),
    ]
