"""Tests for the chat-set LLM API key feature (get/set/mask + DB persistence)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("LLM_API_KEY", "env-key-1234567890abcdef")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import src.llm as llm
from src.llm import get_llm_api_key, mask_key, set_llm_api_key

ENV_KEY = "env-key-1234567890abcdef"


@pytest.fixture(autouse=True)
def _reset_key_state():
    llm._runtime_api_key = None
    llm._consecutive_failures = 0
    llm._quota_cooldown_until = 0.0
    yield
    llm._runtime_api_key = None
    llm._consecutive_failures = 0
    llm._quota_cooldown_until = 0.0


class TestMaskKey:
    def test_long_key_shows_last_4(self):
        assert mask_key("sk-proj-abcdef1234567890") == "...7890"

    def test_short_key_fully_masked(self):
        assert mask_key("short") == "***"


class TestGetLlmApiKey:
    def test_falls_back_to_env_key(self):
        assert get_llm_api_key() == ENV_KEY

    def test_override_wins(self):
        set_llm_api_key("sk-override-key-1234567890")
        assert get_llm_api_key() == "sk-override-key-1234567890"

    def test_clear_override_returns_env_key(self):
        set_llm_api_key("sk-override-key-1234567890")
        set_llm_api_key("")
        assert get_llm_api_key() == ENV_KEY


class TestSetLlmApiKey:
    def test_sets_and_masks(self):
        masked = set_llm_api_key("sk-new-key-abcdefgh12345678")
        assert masked == "...5678"
        assert get_llm_api_key() == "sk-new-key-abcdefgh12345678"

    def test_resets_failure_counters(self):
        llm._consecutive_failures = 5
        llm._quota_cooldown_until = float("inf")
        set_llm_api_key("sk-new-key-abcdefgh12345678")
        assert llm._consecutive_failures == 0
        assert llm._quota_cooldown_until == 0.0

    def test_strips_whitespace(self):
        set_llm_api_key("  sk-padded-key-1234567890  ")
        assert get_llm_api_key() == "sk-padded-key-1234567890"


class TestDbRoundTrip:
    async def test_save_and_load_key(self, tmp_path, monkeypatch):
        import src.db as db

        monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "test.db")
        await db.init_db()
        await db.set_setting(db.LLM_API_KEY_SETTING, "sk-db-key-1234567890")
        assert await db.get_llm_api_key() == "sk-db-key-1234567890"

    async def test_empty_value_returns_none(self, tmp_path, monkeypatch):
        import src.db as db

        monkeypatch.setattr(db, "_get_db_path", lambda: tmp_path / "test.db")
        await db.init_db()
        await db.set_setting(db.LLM_API_KEY_SETTING, "")
        assert await db.get_llm_api_key() is None
