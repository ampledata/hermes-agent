"""SAIL-01 escalation ladder, slice A2: the switch to the local escalation model and back.

No network: the route binder is replaced by a fake that mimics ``bind_route_entry``'s effect on
the agent, and the escalations log goes to a tmp dir.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent  # import at collection time, like the guardrail runtime tests
import agent.escalation_ladder as esc

LANE_MODEL, LANE_URL = "errand-batch", "http://127.0.0.1:4000/v1"
FAIL = json.dumps({"output": "boom", "exit_code": 1, "error": "boom"})
OK = json.dumps({"output": "ok", "exit_code": 0})


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(esc, "_profile_logs_dir", lambda: tmp_path)
    return tmp_path


def _events(log_dir):
    path = log_dir / "escalations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class FakeRoutes:
    """Stands in for bind_route_entry / restore_primary_runtime."""

    def __init__(self, bind_ok=True):
        self.bind_ok, self.bound, self.restored = bind_ok, [], 0

    def bind(self, agent, cfg):
        self.bound.append(cfg)
        if not self.bind_ok:
            return None
        old = (agent.model, agent.provider)
        agent._lane = (agent.model, agent.provider, agent.base_url)
        agent.model, agent.provider, agent.base_url = cfg.model, "custom", cfg.base_url
        agent._fallback_activated = True
        return old

    def restore(self, agent):
        self.restored += 1
        agent.model, agent.provider, agent.base_url = agent._lane
        agent._fallback_activated = False
        return True


@pytest.fixture
def routes(monkeypatch):
    fake = FakeRoutes()
    monkeypatch.setattr(esc, "_bind_escalation_route", fake.bind)
    monkeypatch.setattr(esc, "_restore_lane_route", fake.restore)
    return fake


def _bare_agent(cfg=None):
    agent = SimpleNamespace(session_id="s1", model=LANE_MODEL, provider="custom", base_url=LANE_URL,
                            _fallback_activated=False, _emit_diagnostic_status=lambda msg: None)
    esc.configure_agent(agent, cfg if cfg is not None else {"escalation": {"enabled": True}})
    return agent


def _strike(agent, *failed):
    for f in failed:
        esc.note_tool_outcome(agent, is_error=f)


def test_escalation_switches_to_local_model_after_three_strikes(routes, log_dir, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    agent = _bare_agent()
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    assert (agent.model, agent.base_url) == ("reasoning", "http://127.0.0.1:4000/v1")
    assert routes.bound[0].api_key_env == "LITELLM_MASTER_KEY"
    assert "root cause" in esc.escalation_note(agent)
    [event] = _events(log_dir)
    assert event["event"] == "escalate" and event["strikes"] == 3 and event["card"] == "t_test"
    assert event["from"] == f"{LANE_MODEL}@{LANE_URL}" and event["to"] == "reasoning@http://127.0.0.1:4000/v1"
    assert event["session"] == "s1" and event["dtg"].endswith("Z")


def test_escalation_deescalates_on_first_success(routes, log_dir):
    agent = _bare_agent()
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    _strike(agent, True)  # still failing while escalated: stays escalated, no re-bind
    esc.apply_pending_route(agent)
    assert len(routes.bound) == 1 and agent.model == "reasoning"
    _strike(agent, False)
    esc.apply_pending_route(agent)
    assert agent.model == LANE_MODEL and routes.restored == 1 and esc.escalation_note(agent) == ""
    assert [e["event"] for e in _events(log_dir)] == ["escalate", "deescalate"]


def test_escalation_sticky_stays_for_rest_of_turn(routes, log_dir):
    agent = _bare_agent({"escalation": {"enabled": True, "sticky": True}})
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    _strike(agent, False)
    esc.apply_pending_route(agent)
    assert agent.model == "reasoning" and routes.restored == 0


def test_escalation_refuses_cloud_base_url(routes, log_dir):
    agent = _bare_agent({"escalation": {"enabled": True, "model": "gpt-4o",
                                        "base_url": "https://api.openai.com/v1"}})
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    assert routes.bound == [] and agent.model == LANE_MODEL
    assert agent._escalation_ladder.strikes == 0 and not agent._escalation_ladder.escalated
    [event] = _events(log_dir)
    assert event["event"] == "refused" and event["reason"] == "non-local base_url"


def test_escalation_bind_failure_keeps_lane_and_agent_running(log_dir, monkeypatch):
    fake = FakeRoutes(bind_ok=False)
    monkeypatch.setattr(esc, "_bind_escalation_route", fake.bind)
    agent = _bare_agent()
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    assert agent.model == LANE_MODEL and esc.escalation_note(agent) == ""
    assert _events(log_dir)[0]["event"] == "failed"

    def boom(agent, cfg):
        raise RuntimeError("gx10 down")
    monkeypatch.setattr(esc, "_bind_escalation_route", boom)
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)  # must not raise
    assert agent.model == LANE_MODEL


def test_escalation_noop_when_lane_is_already_escalation_route(routes, log_dir):
    agent = _bare_agent()
    agent.model = "reasoning"
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    assert routes.bound == [] and _events(log_dir) == []


def test_escalation_reset_for_new_turn(routes, log_dir):
    agent = _bare_agent()
    _strike(agent, True, True, True)
    esc.apply_pending_route(agent)
    _strike(agent, True)
    esc.reset_for_new_turn(agent)
    assert agent._escalation_ladder.strikes == 0 and not agent._escalation_ladder.escalated
    assert agent._escalation_pending is None and esc.escalation_note(agent) == ""


def test_escalation_bind_entry_is_local_custom_route(monkeypatch):
    seen = {}

    def fake_bind(agent, entry, provider, model):
        seen.update(entry=entry, provider=provider, model=model)
        return ("x", "y")
    monkeypatch.setattr("agent.route_binding.bind_route_entry", fake_bind)
    cfg = esc.load_escalation_config({"enabled": True})
    assert esc._bind_escalation_route(SimpleNamespace(), cfg) == ("x", "y")
    assert seen == {"provider": "custom", "model": "reasoning", "entry": {
        "provider": "custom", "model": "reasoning", "base_url": "http://127.0.0.1:4000/v1",
        "key_env": "LITELLM_MASTER_KEY"}}


# ── full run_conversation loop with a fake model and fake tools ──

def _tool_defs():
    return [{"type": "function", "function": {"name": "terminal", "description": "t",
                                              "parameters": {"type": "object", "properties": {}}}}]


def _make_agent(config):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url=LANE_URL, model=LANE_MODEL, provider="custom",
                        max_iterations=10, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        platform="cli")
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _tool_response(i):
    tc = SimpleNamespace(id=f"c{i}", type="function",
                         function=SimpleNamespace(name="terminal", arguments=json.dumps({"command": f"x{i}"})))
    msg = SimpleNamespace(content="", tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")], model="m", usage=None)


def _final_response():
    msg = SimpleNamespace(content="done", tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="m", usage=None)


def _run(agent, tool_results):
    sent = []

    def create(**kwargs):
        system = next((m["content"] for m in kwargs["messages"] if m.get("role") == "system"), "")
        sent.append((kwargs["model"], system))
        i = len(sent)
        return _tool_response(i) if i <= len(tool_results) else _final_response()
    agent.client.chat.completions.create.side_effect = create
    results = iter(tool_results)
    with (
        patch("model_tools.handle_function_call", side_effect=lambda *a, **k: next(results)),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the thing")
    return sent, result


def test_escalation_run_conversation_next_call_uses_escalation_model(routes, log_dir):
    agent = _make_agent({"escalation": {"enabled": True}})
    sent, result = _run(agent, [FAIL, FAIL, FAIL, OK])
    models = [m for m, _ in sent]
    assert models == [LANE_MODEL, LANE_MODEL, LANE_MODEL, "reasoning", LANE_MODEL]
    assert "[escalation]" in sent[3][1] and "[escalation]" not in sent[4][1]
    assert result["final_response"] == "done"
    assert [e["event"] for e in _events(log_dir)] == ["escalate", "deescalate"]


def test_escalation_run_conversation_unchanged_when_off(routes, log_dir):
    agent = _make_agent({})
    sent, result = _run(agent, [FAIL, FAIL, FAIL, FAIL])
    assert {m for m, _ in sent} == {LANE_MODEL}
    assert routes.bound == [] and _events(log_dir) == []
    assert result["final_response"] == "done"
