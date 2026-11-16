"""HERMES-GW-BURST-01d: (1) a normal successful turn must survive the real run_turn.py turn-finish
path (01c changed the classifier arity without updating the caller); (2) text-then-photo against the
real session DB: the text is stored once and no failed-turn row appears."""
import asyncio

import pytest

import gateway.run as gateway_run
from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND
from gateway.platforms.event import ProcessingOutcome
from gateway.session import build_session_key
from tests.gateway.test_incomplete_gateway_turns import (
    CaptureSlackAdapter, _make_event, _make_runner,
)
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_normal_successful_turn_finishes_through_run_turn(monkeypatch, tmp_path):
    adapter = CaptureSlackAdapter()
    runner = _make_runner(adapter)
    runner._run_agent = AsyncMock(return_value={
        "final_response": "all good",
        "messages": [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "all good"}],
        "tools": [], "history_offset": 0, "api_calls": 1, "completed": True,
        "interrupted": False, "last_prompt_tokens": 0,
    })
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *_a, **_k: 100)
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "C123")
    adapter.set_message_handler(runner._handle_message)
    adapter._keep_typing = lambda *_a, **_k: asyncio.Event().wait()

    event = _make_event()
    await adapter._process_message_background(event, build_session_key(event.source))

    # F1: a crash in the finish path is turned into an error reply and no assistant row.
    assert [m["content"] for m in adapter.sent] == ["all good"]
    roles = [c.args[1]["role"] for c in runner.session_store.append_to_transcript.call_args_list]
    assert roles == ["session_meta", "user", "assistant"]
    assert adapter.processing_hooks[-1] == ("complete", "m-1", ProcessingOutcome.SUCCESS)


def _persist(runner, store, source, entry, sid, text, mid, result, messages, response, offset):
    from gateway.platforms.event import MessageEvent
    classify = runner._hmwa_classify_turn_failure(result, [], entry)
    return runner._hmwa_persist_turn_transcript(
        event=MessageEvent(text=text, source=source, message_id=mid),
        source=source, session_entry=entry, session_key=entry.session_key, agent_result=result,
        agent_messages=messages, prepared=runner._PreparedTurn([], "", text, None, None, None, sid, "o"),
        response=response, agent_failed_early=classify[0], hidden_reasoning_incomplete=classify[1],
        is_context_overflow_failure=classify[2], superseded=runner._hmwa_turn_superseded(result),
    )


def _rig(tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_db = object()  # agent_persisted defaults True: the agent owns the DB rows

    async def noop(*a, **k):
        return None
    runner._refresh_agent_cache_message_count = noop
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="burst", user_id="u")
    entry = store.get_or_create_session(source)
    return store, runner, source, entry, store._db_for_session_id(entry.session_id)


def test_text_then_photo_real_store_text_stored_once_no_failed_row(tmp_path):
    async def check():
        store, runner, source, entry, db = _rig(tmp_path)
        sid = entry.session_id
        # Turn 1 (text): the agent's turn-start flush persists the user row, then a photo interrupts it.
        db.append_message(sid, "user", "look at this", platform_message_id="m-text")
        superseded = {"failed": True, "interrupted": True, "interrupt_message": "[photo]",
                      "error": "Operation interrupted", "messages": [], "history_offset": 0,
                      "last_prompt_tokens": 0}
        await _persist(runner, store, source, entry, sid, "look at this", "m-text", superseded, [], "", 0)
        # Turn 2 (photo): the agent flushes the photo row at start and its final reply at the end.
        db.append_message(sid, "user", "[photo]", platform_message_id="m-photo")
        db.append_message(sid, "assistant", "a cat")
        ok = {"final_response": "a cat", "interrupted": False, "last_prompt_tokens": 0, "history_offset": 1,
              "messages": [{"role": "user", "content": "look at this"}, {"role": "user", "content": "[photo]"},
                           {"role": "assistant", "content": "a cat"}]}
        await _persist(runner, store, source, entry, sid, "[photo]", "m-photo", ok, ok["messages"], "a cat", 1)
        rows = [m for m in db.get_messages(sid) if m["role"] != "session_meta"]
        print("ROWS", [(m["role"], m["content"], m.get("display_kind")) for m in rows])
        assert [m["content"] for m in rows].count("look at this") == 1
        assert not [m for m in rows if m.get("display_kind") == FAILED_TURN_DISPLAY_KIND]
        db.close()
    asyncio.run(check())


def test_control_without_supersede_writes_failed_row(tmp_path):
    """Negative control: the pre-01b behaviour (burst interrupt treated as failure) does leave a failed
    row in the real store, so the test above discriminates."""
    async def check():
        _store, runner, source, entry, db = _rig(tmp_path)
        sid = entry.session_id
        db.append_message(sid, "user", "look at this", platform_message_id="m-text")
        res = {"failed": True, "interrupted": True, "interrupt_message": "[photo]", "messages": [],
               "history_offset": 0, "last_prompt_tokens": 0}
        from gateway.platforms.event import MessageEvent
        await runner._hmwa_persist_turn_transcript(
            event=MessageEvent(text="look at this", source=source, message_id="m-text"),
            source=source, session_entry=entry, session_key=entry.session_key, agent_result=res,
            agent_messages=[], prepared=runner._PreparedTurn([], "", "look at this", None, None, None, sid, "o"),
            response="", agent_failed_early=True, hidden_reasoning_incomplete=False,
            is_context_overflow_failure=False, superseded=False)
        rows = db.get_messages(sid)
        assert [m for m in rows if m.get("display_kind") == FAILED_TURN_DISPLAY_KIND]
        db.close()
    asyncio.run(check())
