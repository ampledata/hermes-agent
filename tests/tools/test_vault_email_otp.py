"""Email one-time codes for browser_vault enter_code (owner allowlist)."""

import base64
import json
import os
import time

import pytest

from agent.vault_backends import email_otp

RULE = {"name": "caci", "origin": "https://supplier.caci.com", "gmail_token_dir": "/nonexistent",
        "sender": "noreply-CACI-P2P@noreply-p2p.caci.com", "subject_regex": "P2P Verification Code",
        "code_regex": r"\b(\d{6})\b", "max_age_s": 600}


def _msg(mid, sender, subject, body, age_s, now):
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "internalDate": str(int((now - age_s) * 1000)),
            "payload": {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
                        "mimeType": "text/plain", "body": {"data": data}}}


def _fetcher(msgs):
    def fetch(path):
        if path.startswith("messages?"):
            return {"messages": [{"id": m["id"]} for m in msgs]}
        mid = path.split("/")[1].split("?")[0]
        return next(m for m in msgs if m["id"] == mid)
    return fetch


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(email_otp, "_home", lambda: tmp_path)
    return tmp_path


def test_unlisted_origin_returns_none_without_reading_mail():
    def boom(path):
        raise AssertionError("mailbox must not be read for an unlisted site")
    assert email_otp.resolve_email_otp("https://evil.test", rules=[RULE], fetch=boom, wait_s=0) is None


def test_reads_newest_matching_code_once_and_audits_without_code(home):
    now = time.time()
    msgs = [_msg("m2", "Buyer CACI <noreply-CACI-P2P@noreply-p2p.caci.com>", "CACI P2P Verification Code",
                 "Please use the following code to confirm: 482913", 30, now),
            _msg("m1", "Buyer CACI <noreply-CACI-P2P@noreply-p2p.caci.com>", "CACI P2P Verification Code",
                 "code 111111", 200, now)]
    code = email_otp.resolve_email_otp("https://supplier.caci.com", rules=[RULE], fetch=_fetcher(msgs),
                                       wait_s=0, now=lambda: now)
    assert code == "482913"
    log = (home / "logs" / "vault-email-otp.log").read_text()
    assert "m2" in log and "482913" not in log
    # the same message is never used twice; the next unused one is taken instead
    again = email_otp.resolve_email_otp("https://supplier.caci.com", rules=[RULE], fetch=_fetcher(msgs),
                                        wait_s=0, now=lambda: now)
    assert again == "111111"


def test_rejects_wrong_sender_stale_mail_and_wrong_subject():
    now = time.time()
    msgs = [_msg("a", "attacker@evil.test", "CACI P2P Verification Code", "123456", 10, now),
            _msg("b", "noreply-CACI-P2P@noreply-p2p.caci.com", "CACI P2P Verification Code", "234567", 3600, now),
            _msg("c", "noreply-CACI-P2P@noreply-p2p.caci.com", "Newsletter", "345678", 10, now)]
    assert email_otp.resolve_email_otp("https://supplier.caci.com", rules=[RULE], fetch=_fetcher(msgs),
                                       wait_s=0, now=lambda: now) is None


def test_polls_until_code_arrives():
    now = [1_000_000.0]
    msgs = []

    def sleep(s):
        now[0] += s
        msgs.append(_msg("late", "noreply-CACI-P2P@noreply-p2p.caci.com", "CACI P2P Verification Code",
                         "use 777888", 1, now[0]))
    code = email_otp.resolve_email_otp("https://supplier.caci.com", rules=[RULE], fetch=_fetcher(msgs),
                                       wait_s=30, poll_s=5, now=lambda: now[0], sleep=sleep)
    assert code == "777888"


def test_rule_file_writable_by_others_is_ignored(tmp_path):
    p = tmp_path / "vault-email-otp.yaml"
    p.write_text("rules:\n  - origin: https://supplier.caci.com\n    gmail_token_dir: /x\n"
                 "    sender: a@b.c\n    code_regex: '(\\\\d{6})'\n")
    os.chmod(p, 0o600)
    assert len(email_otp.load_rules(p)) == 1
    os.chmod(p, 0o666)
    assert email_otp.load_rules(p) == []
