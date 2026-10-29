"""Profile-bound authenticated subjects for Hermes V2 integration."""

import contextlib

from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


class _LoginSocket:
    def __init__(self, user_id: str = "carol", provider: str = "oidc") -> None:
        self.auth_identity = {"provider": provider, "user_id": user_id}


def test_deferred_session_record_stamps_the_authenticated_subject():
    token = bind_transport(_LoginSocket())
    try:
        record = server._deferred_session_record(
            "deferred-key", cols=80, cwd="/tmp", history=[], lease=None)
    finally:
        reset_transport(token)

    assert record["authenticated_subject"] == 'auth:v1:["oidc","","carol"]'


def test_branch_session_copies_the_authenticated_subject(monkeypatch):
    subject = 'auth:v1:["oidc","","carol"]'
    parent = {
        "authenticated_subject": subject,
        "auth_user_id": "oidc:carol",
        "session_key": "parent-key",
        "cwd": "/tmp",
    }

    monkeypatch.setattr(server, "_profile_build_scope", lambda _home: contextlib.nullcontext())
    monkeypatch.setattr(server, "_session_cwd", lambda _session: "/tmp")
    monkeypatch.setattr(server, "_context_cwd_is_launch_artifact", lambda _session: False)
    monkeypatch.setattr(server, "_make_agent_in_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        server,
        "_init_session",
        lambda sid, *_args, **_kwargs: server._sessions.__setitem__(sid, {}),
    )
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda *_args: None)

    server._build_branch_agent(parent, "child", "child-key", [], "desktop")

    assert server._sessions["child"]["authenticated_subject"] == subject


def test_session_subject_lookup_is_bound_to_the_profile_home(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    subject = 'auth:v1:["oidc","","carol"]'
    monkeypatch.setitem(server._sessions, "sid-profile", {
        "profile_home": str(profile_home),
        "authenticated_subject": subject,
    })

    assert server._session_authenticated_subject("sid-profile", str(profile_home)) == subject
    assert server._session_authenticated_subject("sid-profile", str(tmp_path / "other")) is None
