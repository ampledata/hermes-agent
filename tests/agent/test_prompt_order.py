"""SAIL-01 slice B1: prompt.static_first orders static system content before dynamic content.

Wire-level checks with a fake model client (no network): the request's messages[0] carries only
the stable tier, so the rendered prefix `stable + tools` is byte-identical across turns and
sessions while timestamps, memory and the ephemeral prompt change.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent  # import at collection time, like the guardrail runtime tests
from agent.prompt_order import (
    configure_agent, leading_system_count, static_first_applies, static_first_system_messages,
)
from agent.conversation_loop import _sync_failover_system_message

STATIC = "IDENTITY + GUIDANCE (stable tier)"
DYNAMIC_A = "MEMORY v1\n\nConversation started: Tuesday 09:00"
DYNAMIC_B = "MEMORY v2\n\nConversation started: Wednesday 10:00"


def _bare(static_first=True, api_mode="chat_completions", **kw):
    agent = SimpleNamespace(api_mode=api_mode, provider="custom", _use_native_cache_layout=False,
                            _cached_system_prompt_static=STATIC, ephemeral_system_prompt=None,
                            _escalation_note=None, **kw)
    configure_agent(agent, {"prompt": {"static_first": static_first}})
    return agent


def test_prompt_order_default_off():
    agent = SimpleNamespace(api_mode="chat_completions")
    configure_agent(agent, {})
    assert agent._prompt_static_first is False
    configure_agent(agent, {"prompt": "junk"})
    assert agent._prompt_static_first is False
    assert static_first_system_messages(agent, STATIC + "\n\n" + DYNAMIC_A) is None


def test_prompt_order_splits_static_then_dynamic():
    rows = static_first_system_messages(_bare(), STATIC + "\n\n" + DYNAMIC_A, ("EPHEMERAL", None, ""))
    assert rows == [{"role": "system", "content": STATIC},
                    {"role": "system", "content": DYNAMIC_A + "\n\nEPHEMERAL"}]
    assert leading_system_count(rows + [{"role": "user", "content": "hi"}]) == 2


def test_prompt_order_static_only_prompt_single_row():
    assert static_first_system_messages(_bare(), STATIC) == [{"role": "system", "content": STATIC}]


def test_prompt_order_falls_back_when_prefix_unproven():
    assert static_first_system_messages(_bare(), "rewritten " + STATIC + DYNAMIC_A) is None
    no_static = _bare()
    no_static._cached_system_prompt_static = None
    assert static_first_system_messages(no_static, STATIC + DYNAMIC_A) is None


def test_prompt_order_only_on_chat_completions():
    assert static_first_applies(_bare())
    assert not static_first_applies(_bare(api_mode="anthropic_messages"))
    assert not static_first_applies(_bare(api_mode="codex_responses"))
    native = _bare()
    native._use_native_cache_layout = True
    assert not static_first_applies(native)
    moa = _bare()
    moa.provider = "moa"
    assert not static_first_applies(moa)


def test_prompt_order_failover_sync_keeps_split():
    agent = _bare(_cached_system_prompt=STATIC + "\n\n" + DYNAMIC_B)
    agent.ephemeral_system_prompt = "EPH"
    api_messages = [{"role": "system", "content": STATIC}, {"role": "system", "content": DYNAMIC_A},
                    {"role": "user", "content": "hi"}]
    _sync_failover_system_message(agent, api_messages, STATIC + "\n\n" + DYNAMIC_A)
    assert api_messages == [{"role": "system", "content": STATIC},
                            {"role": "system", "content": DYNAMIC_B + "\n\nEPH"},
                            {"role": "user", "content": "hi"}]
    agent.api_mode = "anthropic_messages"  # failed over to a route where the split does not apply
    _sync_failover_system_message(agent, api_messages, STATIC)
    assert api_messages[0] == {"role": "system", "content": STATIC + "\n\n" + DYNAMIC_B + "\n\nEPH"}
    assert api_messages[1]["role"] == "user"


# ── wire-level, through run_conversation with a fake client ──

def _make_agent(config):
    tool_defs = [{"type": "function", "function": {"name": "terminal", "description": "t",
                                                   "parameters": {"type": "object", "properties": {}}}}]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1:4000/v1", model="errand-batch",
                        provider="custom", max_iterations=5, quiet_mode=True, skip_context_files=True,
                        skip_memory=True, platform="cli")
    agent.client = MagicMock()
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _send(agent, dynamic, ephemeral):
    agent._cached_system_prompt = STATIC + "\n\n" + dynamic
    agent._cached_system_prompt_static = STATIC
    agent.ephemeral_system_prompt = ephemeral
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        msg = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="m", usage=None)
    agent.client.chat.completions.create.side_effect = create
    with (
        patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        agent.run_conversation("hello")
    return sent[-1]


def _prefix(kwargs):
    """What a Gemma-4-style template renders before the conversation: messages[0] then tools."""
    return kwargs["messages"][0]["content"] + json.dumps(kwargs.get("tools"), sort_keys=True)


def test_prompt_order_static_first_wire_prefix_stable_across_turns():
    agent = _make_agent({"prompt": {"static_first": True}})
    assert agent.api_mode == "chat_completions"
    a = _send(agent, DYNAMIC_A, "EPHEMERAL turn 1")
    b = _send(agent, DYNAMIC_B, "EPHEMERAL turn 2")
    assert a["messages"][0] == {"role": "system", "content": STATIC}
    assert a["messages"][1]["role"] == "system" and "MEMORY v1" in a["messages"][1]["content"]
    assert "EPHEMERAL turn 1" in a["messages"][1]["content"]
    assert a["messages"][2]["role"] == "user"
    assert _prefix(a) == _prefix(b)


def test_prompt_order_default_wire_unchanged():
    agent = _make_agent({})
    a = _send(agent, DYNAMIC_A, "EPHEMERAL turn 1")
    b = _send(agent, DYNAMIC_B, "EPHEMERAL turn 2")
    assert a["messages"][0]["content"] == STATIC + "\n\n" + DYNAMIC_A + "\n\nEPHEMERAL turn 1"
    assert a["messages"][1]["role"] == "user"
    assert _prefix(a) != _prefix(b)
