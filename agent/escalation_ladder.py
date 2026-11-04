"""Local-only 3-strike escalation ladder (SAIL-01).

After ``escalation.strikes`` consecutive failed tool results (non-zero exit, exception text
or an error object, as classified by ``agent.display._detect_tool_failure``), the NEXT model
call is routed to ``escalation.model`` at ``escalation.base_url`` with a one-line system note
asking for root-cause diagnosis and the single corrective command. The agent keeps running.
One successful tool result resets the counter and (unless ``escalation.sticky``) returns the
route to the lane model. The next turn always starts on the lane model.

Hard rule (G decision D2): the escalation target must be local. A ``base_url`` whose host is
not loopback/localhost, RFC1918 or the tailnet CGNAT range 100.64.0.0/10 is refused: the hook
logs and stays on the lane model. Hostnames other than ``localhost`` are refused because no
DNS lookup is done on the hot path.

Off by default (``escalation.enabled: false``; ``escalation.strikes: 0`` also disables), so
existing behaviour is unchanged. Every escalate / de-escalate / refusal appends one JSON line
to ``<HERMES_HOME>/logs/escalations.jsonl``.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_STRIKES = 3
DEFAULT_MODEL = "reasoning"
DEFAULT_BASE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_API_KEY_ENV = "LITELLM_MASTER_KEY"
ESCALATION_NOTE = (
    "[escalation] The last {strikes} tool calls failed in a row; you are now the escalation model. "
    "State the root cause in one sentence, then issue the single corrective command."
)

_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # loopback + RFC1918
    "100.64.0.0/10",  # tailnet (CGNAT)
    "::1/128", "fd7a:115c:a1e0::/48",  # IPv6 loopback + Tailscale ULA
))


@dataclass(frozen=True)
class EscalationConfig:
    enabled: bool = False
    strikes: int = DEFAULT_STRIKES
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    sticky: bool = False

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.strikes > 0 and self.model and self.base_url)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def load_escalation_config(raw: Any) -> EscalationConfig:
    """Parse the ``escalation:`` config section; malformed values fall back to defaults."""
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    try:
        strikes = int(section.get("strikes", DEFAULT_STRIKES))
    except (TypeError, ValueError):
        strikes = DEFAULT_STRIKES
    return EscalationConfig(
        enabled=_truthy(section.get("enabled", False)),
        strikes=max(0, strikes),
        model=str(section.get("model") or DEFAULT_MODEL).strip(),
        base_url=str(section.get("base_url") or DEFAULT_BASE_URL).strip(),
        api_key_env=str(section.get("api_key_env") or DEFAULT_API_KEY_ENV).strip(),
        sticky=_truthy(section.get("sticky", False)),
    )


def is_local_base_url(url: Any) -> bool:
    """True only for literal loopback/RFC1918/tailnet hosts or ``localhost``."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        host = urlparse(url.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # non-literal hostname: refused (no DNS on the hot path)
    return any(addr in net for net in _LOCAL_NETWORKS if net.version == addr.version)


class StrikeLadder:
    """Pure consecutive-failure counter. ``record`` returns ``"escalate"``, ``"deescalate"`` or None."""

    def __init__(self, config: EscalationConfig):
        self.config = config
        self.strikes = 0
        self.escalated = False

    def record(self, failed: bool) -> Optional[str]:
        if failed:
            self.strikes += 1
            if not self.escalated and self.strikes >= self.config.strikes:
                self.escalated = True  # optimistic; apply_pending_route reverts on refusal
                return "escalate"
            return None
        self.strikes = 0
        if self.escalated and not self.config.sticky:
            self.escalated = False
            return "deescalate"
        return None

    def reset(self) -> None:
        self.strikes = 0
        self.escalated = False


# ── agent integration ────────────────────────────────────────────────────────

def configure_agent(agent: Any, agent_cfg: Mapping[str, Any] | None) -> None:
    """Attach the ladder (or None when off) at agent init."""
    cfg_dict = agent_cfg if isinstance(agent_cfg, Mapping) else {}
    cfg = load_escalation_config(cfg_dict.get("escalation"))
    agent._escalation_ladder = StrikeLadder(cfg) if cfg.active else None
    agent._escalation_pending = None
    agent._escalation_note = None
    if cfg.active and not is_local_base_url(cfg.base_url):
        logger.warning("escalation.base_url is not a local address; the ladder will refuse to escalate")


_APPROVAL_STATUSES = frozenset({"blocked", "pending_approval", "approval_required"})


def is_approval_outcome(result: Any) -> bool:
    """True when a tool result is a human approval outcome (denied, pending, required).

    Approval payloads come back from inside the dispatched tool as error JSON, so they look like
    failures to ``_detect_tool_failure``. A human "no" is not a model mistake and must not count
    as a strike; escalating on it would tell the stronger model to "issue the corrective command".
    """
    if not isinstance(result, str) or not result.lstrip().startswith("{"):
        return False
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("status") in _APPROVAL_STATUSES:
        return True
    if payload.get("approval_pending") is True or payload.get("user_consent") is False:
        return True
    return False


def note_tool_outcome(agent: Any, *, is_error: bool, blocked: bool = False, result: Any = None) -> None:
    """Count one committed tool result.

    Blocked calls never ran and do not count either way. Approval outcomes (a denied, pending or
    required human approval) are a human decision, not a tool failure, and do not count either.
    """
    ladder = getattr(agent, "_escalation_ladder", None)
    if ladder is None or blocked:
        return
    if is_error and is_approval_outcome(result):
        return
    action = ladder.record(bool(is_error))
    if action is not None:
        agent._escalation_pending = action


def _profile_logs_dir():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "logs"


def _route_label(model: Any, base_url: Any) -> str:
    return f"{model or '?'}@{base_url or '?'}"


def _log_event(agent: Any, event: str, *, strikes: int, from_route: str, to_route: str, reason: str = "") -> None:
    record = {
        "event": event,
        "session": getattr(agent, "session_id", None),
        "card": os.environ.get("HERMES_KANBAN_TASK") or None,
        "strikes": strikes,
        "from": from_route,
        "to": to_route,
        "dtg": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if reason:
        record["reason"] = reason
    try:
        logs = _profile_logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "escalations.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        logger.debug("escalations.jsonl write failed", exc_info=True)


def _bind_escalation_route(agent: Any, cfg: EscalationConfig):
    """Swap the agent onto the escalation route via the fallback binder (same client/caps
    plumbing as a provider fallback; ``_fallback_activated`` makes the next turn restore)."""
    from agent.route_binding import bind_route_entry
    entry = {"provider": "custom", "model": cfg.model, "base_url": cfg.base_url, "key_env": cfg.api_key_env}
    return bind_route_entry(agent, entry, "custom", cfg.model)


def _restore_lane_route(agent: Any) -> bool:
    restore = getattr(agent, "_restore_primary_runtime", None)
    return bool(restore()) if callable(restore) else False


def _escalate(agent: Any, ladder: StrikeLadder) -> None:
    cfg = ladder.config
    from_route = _route_label(getattr(agent, "model", None), getattr(agent, "base_url", None))
    to_route = _route_label(cfg.model, cfg.base_url)
    strikes = ladder.strikes
    if getattr(agent, "model", None) == cfg.model and getattr(agent, "base_url", None) == cfg.base_url:
        ladder.reset()  # the lane already runs on the escalation route: nothing to climb to
        return
    if not is_local_base_url(cfg.base_url):
        logger.warning("Escalation refused: %s is not a local URL (G D2); staying on %s", cfg.base_url, from_route)
        ladder.reset()
        _log_event(agent, "refused", strikes=strikes, from_route=from_route, to_route=to_route,
                   reason="non-local base_url")
        return
    try:
        bound = _bind_escalation_route(agent, cfg)
    except Exception as exc:  # a failed swap must never stop the agent
        logger.warning("Escalation to %s failed: %s; staying on %s", to_route, exc, from_route)
        bound = None
    if bound is None:
        ladder.reset()
        _log_event(agent, "failed", strikes=strikes, from_route=from_route, to_route=to_route,
                   reason="escalation route unavailable")
        return
    agent._escalation_note = ESCALATION_NOTE.format(strikes=strikes)
    agent._escalation_from_route = from_route
    logger.info("Escalation ladder: %d consecutive tool failures, %s -> %s", strikes, from_route, to_route)
    _log_event(agent, "escalate", strikes=strikes, from_route=from_route, to_route=to_route)
    with suppress(Exception):
        agent._emit_diagnostic_status(f"Escalation: {strikes} tool failures in a row; next call uses {cfg.model}.")


def _deescalate(agent: Any) -> None:
    from_route = _route_label(getattr(agent, "model", None), getattr(agent, "base_url", None))
    agent._escalation_note = None
    restored = False
    try:
        restored = _restore_lane_route(agent)
    except Exception as exc:
        logger.warning("Escalation de-escalate failed: %s", exc)
    to_route = _route_label(getattr(agent, "model", None), getattr(agent, "base_url", None))
    _log_event(agent, "deescalate", strikes=0, from_route=from_route,
               to_route=to_route if restored else getattr(agent, "_escalation_from_route", to_route),
               reason="" if restored else "lane restore deferred to next turn")


def apply_pending_route(agent: Any) -> None:
    """Called before the next request is built (nothing in flight): apply a queued switch."""
    action = getattr(agent, "_escalation_pending", None)
    agent._escalation_pending = None
    ladder = getattr(agent, "_escalation_ladder", None)
    if ladder is None or action is None:
        return
    if action == "escalate":
        _escalate(agent, ladder)
    elif action == "deescalate":
        _deescalate(agent)


def reset_for_new_turn(agent: Any) -> None:
    """Turn start (after the primary-runtime restore): clear strikes, note and queued switch."""
    ladder = getattr(agent, "_escalation_ladder", None)
    if ladder is not None:
        ladder.reset()
    agent._escalation_pending = None
    agent._escalation_note = None


def escalation_note(agent: Any) -> str:
    note = getattr(agent, "_escalation_note", None)
    return note if isinstance(note, str) else ""


__all__ = [
    "EscalationConfig", "StrikeLadder", "apply_pending_route", "configure_agent", "escalation_note",
    "is_local_base_url", "load_escalation_config", "note_tool_outcome", "reset_for_new_turn",
]
