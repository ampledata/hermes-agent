"""Static-first system prompt ordering (SAIL-01, ``prompt.static_first``).

Chat templates such as Gemma-4 render ``messages[0]`` (the system prompt) and THEN the tool
declarations inside the same system turn. Any per-session or per-turn byte in the system prompt
(timestamp, memory, profile line, kanban task brief, ``ephemeral_system_prompt``) therefore sits
ahead of the multi-thousand-token tool block, and a longest-prefix cache (vLLM / llama.cpp)
re-prefills the tools whenever that byte changes.

With ``prompt.static_first: true`` on a chat-completions route the request carries two system
messages: ``messages[0]`` holds only the stable tier (identity, guidance, coding brief; the same
bytes across sessions and profiles), and a second system message right after it holds the
context + volatile tiers, the ephemeral prompt and the escalation note. The rendered prefix is
then ``stable + tools`` for every request. Off by default; other api_modes (Anthropic native
cache layout, Responses, Bedrock, MoA) keep the single concatenated system message.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def configure_agent(agent: Any, agent_cfg: Any) -> None:
    section = agent_cfg.get("prompt") if isinstance(agent_cfg, dict) else None
    agent._prompt_static_first = _truthy(section.get("static_first", False)) if isinstance(section, dict) else False


def static_first_applies(agent: Any) -> bool:
    return bool(
        getattr(agent, "_prompt_static_first", False)
        and getattr(agent, "api_mode", None) == "chat_completions"
        and not getattr(agent, "_use_native_cache_layout", False)
        and getattr(agent, "provider", None) != "moa"
    )


def static_first_system_messages(
    agent: Any, active_system_prompt: Any, extras: Iterable[Any] = (),
) -> Optional[list[dict[str, str]]]:
    """Leading system rows in static-first order, or None to keep the legacy single row.

    Splits only when the prompt provably starts with the stable tier
    (``agent._cached_system_prompt_static``); a rewritten or restored prompt that does not
    falls back to the legacy layout rather than guessing a boundary.
    """
    if not static_first_applies(agent) or not isinstance(active_system_prompt, str):
        return None
    static = getattr(agent, "_cached_system_prompt_static", None)
    if not isinstance(static, str) or not static.strip() or not active_system_prompt.startswith(static):
        return None
    dynamic_parts = [active_system_prompt[len(static):].strip()]
    dynamic_parts.extend(e.strip() for e in extras if isinstance(e, str) and e.strip())
    dynamic = "\n\n".join(p for p in dynamic_parts if p)
    rows = [{"role": "system", "content": static.strip()}]
    if dynamic:
        rows.append({"role": "system", "content": dynamic})
    return rows


def leading_system_count(api_messages: Any) -> int:
    n = 0
    for msg in api_messages or ():
        if not (isinstance(msg, dict) and msg.get("role") == "system"):
            break
        n += 1
    return n


__all__ = ["configure_agent", "leading_system_count", "static_first_applies", "static_first_system_messages"]
