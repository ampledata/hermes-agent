"""Email one-time codes for ``browser_vault enter_code`` (opt-in, per-site allowlist).

Some sites email the second-factor code instead of supporting an authenticator app.
When the owner has listed a site in ``<HERMES_HOME>/vault-email-otp.yaml``, enter_code
reads the newest matching message from that mailbox *server-side* and fills the code
through the same supervisor path as a TOTP code. The code never enters the
conversation, tool output or logs. Sites that are not listed still prompt the user.

Rule file (mode 0600; only the owner edits it, never the agent):

    rules:
      - name: caci-supplier
        origin: https://supplier.caci.com        # exact page origin, no wildcards
        gmail_token_dir: /home/hermes/google-accounts/llc-rw   # token.json + client.json
        sender: noreply-CACI-P2P@noreply-p2p.caci.com          # exact From address
        subject_regex: "P2P Verification Code"
        code_regex: "\\b(\\d{6})\\b"                # group 1 = the code
        max_age_s: 600                               # ignore older mail

Every use appends one line to ``<HERMES_HOME>/logs/vault-email-otp.log``: time, rule,
origin, message id. The code itself is not written. A message id is used only once.
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _rules_path() -> Path:
    return _home() / "vault-email-otp.yaml"


def load_rules(path: Optional[Path] = None) -> list[dict]:
    p = path or _rules_path()
    if not p.exists():
        return []
    # Refuse a rule file others can write: the allowlist is the owner's control.
    if p.stat().st_mode & 0o022:
        return []
    # Core ships ruamel.yaml (via utils), not PyYAML, since v0.23.
    from utils import fast_safe_load
    data = fast_safe_load(p.read_text(encoding="utf-8")) or {}
    out = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict):
            continue
        if not all(r.get(k) for k in ("origin", "gmail_token_dir", "sender", "code_regex")):
            continue
        out.append(r)
    return out


def rule_for_origin(origin: str, rules: Optional[list[dict]] = None) -> Optional[dict]:
    for r in rules if rules is not None else load_rules():
        if r["origin"].rstrip("/") == (origin or "").rstrip("/"):
            return r
    return None


def _access_token(token_dir: str) -> str:
    d = Path(token_dir)
    tok = json.loads((d / "token.json").read_text())
    client = json.loads((d / "client.json").read_text())
    data = urllib.parse.urlencode({"client_id": client["client_id"], "client_secret": client["client_secret"],
                                   "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}).encode()
    uri = client.get("token_uri", "https://oauth2.googleapis.com/token")
    return json.load(urllib.request.urlopen(uri, data=data, timeout=15))["access_token"]


def _gmail_fetch_factory(token_dir: str) -> Callable[[str], dict]:
    at = _access_token(token_dir)

    def fetch(path: str) -> dict:
        req = urllib.request.Request(GMAIL + path, headers={"Authorization": "Bearer " + at})
        return json.load(urllib.request.urlopen(req, timeout=20))
    return fetch


def _message_text(msg: dict) -> str:
    parts: list[str] = []

    def walk(p: dict) -> None:
        if p.get("parts"):
            for q in p["parts"]:
                walk(q)
        elif p.get("body", {}).get("data") and p.get("mimeType") in ("text/plain", "text/html"):
            t = base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
            if p["mimeType"] == "text/html":
                t = re.sub(r"<[^>]+>", " ", t)
            parts.append(html.unescape(t))
    walk(msg.get("payload", {}))
    return " ".join(parts) or msg.get("snippet", "")


def _used_path() -> Path:
    return _home() / "cache" / "vault-email-otp-used.json"


def _audit(rule: dict, origin: str, msg_id: str) -> None:
    log = _home() / "logs" / "vault-email-otp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": rule.get("name"),
                            "origin": origin, "message_id": msg_id}) + "\n")


def resolve_email_otp(origin: str, *, rules: Optional[list[dict]] = None,
                      fetch: Optional[Callable[[str], dict]] = None,
                      wait_s: float = 90.0, poll_s: float = 5.0, now: Callable[[], float] = time.time,
                      sleep: Callable[[float], None] = time.sleep) -> Optional[str]:
    """Return the newest unused code for ``origin`` from its allowlisted mailbox, else None."""
    rule = rule_for_origin(origin, rules)
    if rule is None:
        return None
    fetch = fetch or _gmail_fetch_factory(rule["gmail_token_dir"])
    max_age = float(rule.get("max_age_s", 600))
    code_re = re.compile(rule["code_regex"])
    subj_re = re.compile(rule["subject_regex"]) if rule.get("subject_regex") else None
    used_p = _used_path()
    used = set(json.loads(used_p.read_text())) if used_p.exists() else set()
    q = urllib.parse.urlencode({"q": f"from:{rule['sender']} newer_than:1d", "maxResults": 5})
    deadline = now() + wait_s
    while True:
        for m in fetch("messages?" + q).get("messages", []):
            if m["id"] in used:
                continue
            msg = fetch(f"messages/{m['id']}?format=full")
            if now() - int(msg.get("internalDate", 0)) / 1000 > max_age:
                continue
            hdr = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            if rule["sender"].lower() not in hdr.get("from", "").lower():
                continue
            if subj_re and not subj_re.search(hdr.get("subject", "")):
                continue
            hit = code_re.search(_message_text(msg))
            if not hit:
                continue
            used.add(m["id"])
            used_p.parent.mkdir(parents=True, exist_ok=True)
            used_p.write_text(json.dumps(sorted(used)[-200:]))
            os.chmod(used_p, 0o600)
            _audit(rule, origin, m["id"])
            return hit.group(1) if hit.groups() else hit.group(0)
        if now() >= deadline:
            return None
        sleep(poll_s)
