"""SAIL-01 escalation ladder, slice A1: strike counter, config parsing and the local-URL rule.

No network: tool outcomes come from a fake tool runner whose results go through the real
``_detect_tool_failure`` classifier, then into ``note_tool_outcome`` exactly as
``_commit_tool_result`` feeds it.
"""
import json
from types import SimpleNamespace

import pytest

from run_agent import AIAgent  # import at collection time, like the guardrail runtime tests
from agent.display import _detect_tool_failure
from agent.escalation_ladder import (
    DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL, DEFAULT_MODEL, DEFAULT_STRIKES,
    EscalationConfig, StrikeLadder, configure_agent, is_local_base_url, load_escalation_config,
    note_tool_outcome,
)

ON = {"escalation": {"enabled": True}}

FAIL_EXIT = ("terminal", json.dumps({"output": "boom", "exit_code": 1, "error": "boom"}))
FAIL_OBJ = ("read_file", json.dumps({"success": False, "error": "No such file"}))
FAIL_EXC = ("web_extract", "Error executing tool 'web_extract': ConnectionError")
OK = ("terminal", json.dumps({"output": "ok", "exit_code": 0}))


def _agent(cfg=ON):
    agent = SimpleNamespace(session_id="s-test", model="errand-batch", base_url="http://127.0.0.1:4000/v1")
    configure_agent(agent, cfg)
    return agent


def _run(agent, *results, blocked=False):
    """Fake tool runner: classify each result like the executor, then count it."""
    for name, result in results:
        failed, _ = _detect_tool_failure(name, result)
        note_tool_outcome(agent, is_error=failed, blocked=blocked)


def test_strike_defaults_ladder_off():
    cfg = load_escalation_config(None)
    assert cfg == EscalationConfig()
    assert (cfg.enabled, cfg.strikes, cfg.model, cfg.base_url, cfg.api_key_env, cfg.sticky) == (
        False, DEFAULT_STRIKES, DEFAULT_MODEL, DEFAULT_BASE_URL, DEFAULT_API_KEY_ENV, False)
    assert (DEFAULT_STRIKES, DEFAULT_MODEL, DEFAULT_BASE_URL, DEFAULT_API_KEY_ENV) == (
        3, "reasoning", "http://127.0.0.1:4000/v1", "LITELLM_MASTER_KEY")
    agent = _agent({})
    assert agent._escalation_ladder is None
    _run(agent, FAIL_EXIT, FAIL_EXIT, FAIL_EXIT, FAIL_EXIT)
    assert agent._escalation_pending is None


def test_strike_zero_disables_even_when_enabled():
    agent = _agent({"escalation": {"enabled": True, "strikes": 0}})
    assert agent._escalation_ladder is None


def test_strike_malformed_values_fall_back():
    cfg = load_escalation_config({"enabled": "yes", "strikes": "lots", "model": "", "base_url": None})
    assert cfg.enabled and cfg.strikes == 3 and cfg.model == "reasoning" and cfg.base_url == DEFAULT_BASE_URL


def test_strike_two_failures_no_escalation():
    agent = _agent()
    _run(agent, FAIL_EXIT, FAIL_OBJ)
    assert agent._escalation_ladder.strikes == 2
    assert agent._escalation_pending is None


def test_strike_three_failures_queue_escalation():
    agent = _agent()
    _run(agent, FAIL_EXIT, FAIL_OBJ, FAIL_EXC)
    assert agent._escalation_ladder.strikes == 3
    assert agent._escalation_pending == "escalate"


def test_strike_success_resets_counter():
    agent = _agent()
    _run(agent, FAIL_EXIT, FAIL_EXIT, OK, FAIL_EXIT, FAIL_EXIT)
    assert agent._escalation_ladder.strikes == 2
    assert agent._escalation_pending is None


def test_strike_custom_threshold():
    agent = _agent({"escalation": {"enabled": True, "strikes": 2}})
    _run(agent, FAIL_EXIT, FAIL_EXIT)
    assert agent._escalation_pending == "escalate"


def test_strike_blocked_calls_do_not_count():
    agent = _agent()
    _run(agent, FAIL_EXIT, FAIL_EXIT)
    _run(agent, FAIL_EXIT, blocked=True)
    assert agent._escalation_ladder.strikes == 2 and agent._escalation_pending is None


def test_strike_ladder_escalates_once_then_deescalates_on_success():
    ladder = StrikeLadder(EscalationConfig(enabled=True))
    assert [ladder.record(True) for _ in range(5)] == [None, None, "escalate", None, None]
    assert ladder.record(False) == "deescalate"
    assert ladder.strikes == 0 and not ladder.escalated


def test_strike_sticky_keeps_escalation_after_success():
    ladder = StrikeLadder(EscalationConfig(enabled=True, sticky=True))
    for _ in range(3):
        ladder.record(True)
    assert ladder.record(False) is None and ladder.escalated and ladder.strikes == 0


@pytest.mark.parametrize("url,local", [
    ("http://127.0.0.1:4000/v1", True),
    ("http://localhost:4000/v1", True),
    ("http://172.25.30.239:4000/v1", True),
    ("http://10.0.0.5:8000/v1", True),
    ("http://192.168.1.10:11434/v1", True),
    ("http://100.82.77.22:8081/v1", True),  # tailnet 100.64/10
    ("http://[::1]:4000/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://api.anthropic.com", False),
    ("https://openrouter.ai/api/v1", False),
    ("http://8.8.8.8/v1", False),
    ("http://100.128.0.1/v1", False),  # just outside 100.64/10
    ("http://172.32.0.1/v1", False),  # just outside 172.16/12
    ("http://brute.lan:4000/v1", False),  # hostnames are not resolved: refused
    ("", False),
    (None, False),
])
def test_escalation_local_url_rule(url, local):
    assert is_local_base_url(url) is local


# ── wired through the real sequential executor (fake tool dispatch, no network) ──

def _real_agent(config):
    from unittest.mock import MagicMock, patch
    tool_defs = [{"type": "function", "function": {"name": "terminal", "description": "t",
                                                    "parameters": {"type": "object", "properties": {}}}}]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1:4000/v1",
                        model="errand-batch", provider="custom", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, platform="cli")
    agent.client = MagicMock()
    return agent


def _call(i):
    return SimpleNamespace(id=f"c{i}", type="function",
                           function=SimpleNamespace(name="terminal", arguments=json.dumps({"command": f"x{i}"})))


def test_strike_counter_wired_into_sequential_executor():
    from unittest.mock import patch
    agent = _real_agent(ON)
    assert isinstance(agent._escalation_ladder, StrikeLadder)
    results = iter([FAIL_EXIT[1], FAIL_EXIT[1], OK[1], FAIL_EXIT[1], FAIL_EXIT[1], FAIL_EXIT[1]])
    messages = []
    with patch("model_tools.handle_function_call", side_effect=lambda *a, **k: next(results)):
        for i in range(6):
            agent._execute_tool_calls_sequential(SimpleNamespace(content="", tool_calls=[_call(i)]), messages, "t1")
            if i == 2:
                assert agent._escalation_ladder.strikes == 0
    assert agent._escalation_ladder.strikes == 3
    assert agent._escalation_pending == "escalate"


def test_strike_counter_absent_when_ladder_off():
    agent = _real_agent({})
    assert agent._escalation_ladder is None


def test_escalation_config_defaults_match_default_config():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    section = DEFAULT_CONFIG["escalation"]
    assert load_escalation_config(section) == EscalationConfig()  # shipped config keeps the ladder off
    assert section == {"enabled": False, "strikes": 3, "model": "reasoning",
                       "base_url": "http://127.0.0.1:4000/v1", "api_key_env": "LITELLM_MASTER_KEY",
                       "sticky": False}
    assert is_local_base_url(section["base_url"])
    assert DEFAULT_CONFIG["prompt"] == {"static_first": False}
