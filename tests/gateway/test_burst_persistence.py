import time
import asyncio
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock
from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
from gateway.run import _CONTROL_INTERRUPT_MESSAGES

class MockStore:
    def __init__(self):
        self.transcript = []
        self.platform_messages = set()

    async def append_to_transcript(self, sid, entry, skip_db=False):
        self.transcript.append(entry)

    async def has_platform_message_id(self, sid, msg_id):
        return msg_id in self.platform_messages

    async def update_session(self, key, **kwargs):
        pass

class MockSessionEntry:
    def __init__(self, sid="test-session"):
        self.session_id = sid
        self.session_key = f"key-{sid}"

class MockPrepared:
    def __init__(self, history=None):
        self.history = history or []
        self.persist_user_display_kind = "default"

class MockEvent:
    def __init__(self, message_id=123):
        self.message_id = message_id
        self.reply_expected = True

class TestBurstPersistence(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MockStore()
        class PersistenceProxy(GatewayTurnPersistenceMixin):
            def __init__(self):
                self.async_session_store = None
                self._session_db = None
        
        self.persistence = PersistenceProxy()
        self.persistence.async_session_store = self.store
        
        self.session_entry = MockSessionEntry()
        self.prepared = MockPrepared()
        self.event = MockEvent()
        
        self.persistence._hmwa_user_transcript_entry = MagicMock(return_value={"role": "user", "content": "hello"})
        self.persistence._hmwa_failed_turn_notice = MagicMock(return_value="not processed")
        self.persistence._hmwa_close_failed_turn = AsyncMock()
        self.persistence._refresh_agent_cache_message_count = AsyncMock()

    async def test_burst_persistence_logic(self):
        # --- Test (a): Superseded (Burst) ---
        agent_result_superseded = {
            "interrupted": True,
            "interrupt_message": "superseded by a new live turn",
            "failed": False,
        }
        
        failed_early, hidden, overflow = self.persistence._hmwa_classify_turn_failure(
            agent_result_superseded, self.prepared.history, self.session_entry
        )
        self.assertTrue(self.persistence._hmwa_turn_superseded(agent_result_superseded))
        self.assertFalse(failed_early)
        
        self.store.transcript = []
        await self.persistence._hmwa_persist_turn_transcript(
            event=self.event, source=MagicMock(platform=None), session_entry=self.session_entry, session_key=self.session_entry.session_key,
            agent_result=agent_result_superseded, agent_messages=[], prepared=self.prepared,
            response=None, agent_failed_early=failed_early,
            hidden_reasoning_incomplete=hidden, is_context_overflow_failure=overflow,
            superseded=self.persistence._hmwa_turn_superseded(agent_result_superseded),
        )
        self.assertEqual(len(self.store.transcript), 0)

        # --- Test (b): Negative Control (Gateway Restart) ---
        # Use the EXACT string from the code to ensure it matches _CONTROL_INTERRUPT_MESSAGES
        agent_result_restart = {
            "interrupted": True,
            "interrupt_message": "Gateway restarting",
            "failed": True,
        }
        
        failed_early, hidden, overflow = self.persistence._hmwa_classify_turn_failure(
            agent_result_restart, self.prepared.history, self.session_entry
        )
        self.assertFalse(self.persistence._hmwa_turn_superseded(agent_result_restart))
        self.assertTrue(failed_early)
        
        self.store.transcript = []
        await self.persistence._hmwa_persist_turn_transcript(
            event=self.event, source=MagicMock(platform=None), session_entry=self.session_entry, session_key=self.session_entry.session_key,
            agent_result=agent_result_restart, agent_messages=[], prepared=self.prepared,
            response=None, agent_failed_early=failed_early,
            hidden_reasoning_incomplete=hidden, is_context_overflow_failure=overflow,
            superseded=self.persistence._hmwa_turn_superseded(agent_result_restart),
        )
        self.assertGreaterEqual(len(self.store.transcript), 1)
        self.persistence._hmwa_close_failed_turn.assert_called()

        # --- Test (c): Transcript dedupe (Text -> Photo Burst) ---
        self.store.transcript = []
        
        # Turn 1: Text (superseded)
        text_result = {"interrupted": True, "interrupt_message": "superseded by a new live turn", "failed": False}
        f1, h1, o1 = self.persistence._hmwa_classify_turn_failure(text_result, self.prepared.history, self.session_entry)
        await self.persistence._hmwa_persist_turn_transcript(
            event=self.event, source=MagicMock(platform=None), session_entry=self.session_entry, session_key=self.session_entry.session_key,
            agent_result=text_result, agent_messages=[], prepared=self.prepared,
            response=None, agent_failed_early=f1, hidden_reasoning_incomplete=h1, is_context_overflow_failure=o1,
            superseded=self.persistence._hmwa_turn_superseded(text_result),
        )
        
        # Turn 2: Photo (merged)
        merged_messages = [
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "photo.jpg"},
            {"role": "assistant", "content": "I see the photo!"}
        ]
        photo_result = {"interrupted": False, "failed": False}
        f2, h2, o2 = self.persistence._hmwa_classify_turn_failure(photo_result, self.prepared.history, self.session_entry)
        await self.persistence._hmwa_persist_turn_transcript(
            event=self.event, source=MagicMock(platform=None), session_entry=self.session_entry, session_key=self.session_entry.session_key,
            agent_result=photo_result, agent_messages=merged_messages, prepared=self.prepared,
            response="I see the photo!", agent_failed_early=f2, hidden_reasoning_incomplete=h2, is_context_overflow_failure=o2,
            superseded=self.persistence._hmwa_turn_superseded(photo_result),
        )
        
        user_texts = [entry.get("content") for entry in self.store.transcript if entry.get("role") == "user"]
        self.assertEqual(user_texts.count("hello"), 1)
        self.assertFalse(any("not processed" in str(entry) for entry in self.store.transcript))

        # --- Test (d): Real failure still gets notice ---
        self.store.transcript = []
        fail_result = {"failed": True, "interrupted": False}
        f3, h3, o3 = self.persistence._hmwa_classify_turn_failure(fail_result, self.prepared.history, self.session_entry)
        self.assertTrue(f3)
        await self.persistence._hmwa_persist_turn_transcript(
            event=self.event, source=MagicMock(platform=None), session_entry=self.session_entry, session_key=self.session_entry.session_key,
            agent_result=fail_result, agent_messages=[], prepared=self.prepared,
            response=None, agent_failed_early=f3, hidden_reasoning_incomplete=h3, is_context_overflow_failure=o3,
            superseded=self.persistence._hmwa_turn_superseded(fail_result),
        )
        self.persistence._hmwa_close_failed_turn.assert_called()
