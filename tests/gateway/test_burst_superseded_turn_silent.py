"""HERMES-GW-BURST-01b: a text turn interrupted by a photo burst (superseded/merged into the
next turn) must not carry the 'not processed' failure notice."""
import unittest
from types import SimpleNamespace

from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from agent.turn_failure_copy import FAILED_TURN_NOTICE


def _runner():
    return GatewayTurnPersistenceMixin()


def _notice_for(agent_result):
    """Mirror gateway/run_turn.py: notice is appended iff the classifier says failed-early."""
    r = _runner()
    entry = SimpleNamespace(session_id="s1")
    failed, _hidden, overflow = r._hmwa_classify_turn_failure(agent_result, [], entry)
    if failed and not overflow:
        return r._hmwa_add_failed_turn_notice("", FAILED_TURN_NOTICE)
    return ""


class BurstSupersedeTests(unittest.TestCase):
    def test_text_superseded_by_photo_burst_is_silent(self):
        # Turn 1 (text) cancelled inside the batching window by the arriving photo.
        first = {"failed": True, "interrupted": True, "interrupt_message": "[photo]",
                 "completed": False, "error": "Operation interrupted", "messages": []}
        self.assertEqual(_notice_for(first), "")

    def test_real_failure_still_gets_notice(self):
        self.assertIn(FAILED_TURN_NOTICE, _notice_for({"failed": True, "error": "ReadTimeout", "messages": []}))


if __name__ == "__main__":
    unittest.main()
