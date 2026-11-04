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


def note_tool_outcome(agent: Any, *, is_error: bool, blocked: bool = False) -> None:
    """Count one committed tool result. Blocked calls never ran and do not count either way."""
    ladder = getattr(agent, "_escalation_ladder", None)
    if ladder is None or blocked:
        return
    action = ladder.record(bool(is_error))
    if action is not None:
        agent._escalation_pending = action


__all__ = [
    "EscalationConfig", "StrikeLadder", "configure_agent", "is_local_base_url",
    "load_escalation_config", "note_tool_outcome",
]
