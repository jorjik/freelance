"""Tests for the shared _ping_llm_api helper used by /status and /set_key."""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("LLM_API_KEY", "env-key-1234567890abcdef")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

import src.llm as llm
from src.bot import _ping_llm_api
from src.config import settings
from src.llm import get_llm_api_key, set_llm_api_key


def _response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    return resp


@pytest.fixture(autouse=True)
def _reset_key_state():
    llm._runtime_api_key = None
    yield
    llm._runtime_api_key = None


def _fake_async_client(resp=None, exc=None):
    """Build a mock so `async with httpx.AsyncClient() as c: c.get(...)` works."""
    if exc is not None:
        get_mock = AsyncMock(side_effect=exc)
    else:
        get_mock = AsyncMock(return_value=resp)

    def _factory(*args, **kwargs):
        client = MagicMock()
        client.get = get_mock
        return _async_ctx(client)

    return patch("src.bot.httpx.AsyncClient", side_effect=_factory), get_mock


class _async_ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *a):
        return False


class TestPingLlmApi:
    async def test_200_is_green(self):
        patcher, _ = _fake_async_client(resp=_response(200))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🟢", 200, None)

    async def test_401_is_red(self):
        patcher, _ = _fake_async_client(resp=_response(401))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🔴", 401, None)

    async def test_402_is_red(self):
        patcher, _ = _fake_async_client(resp=_response(402))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🔴", 402, None)

    async def test_500_is_yellow(self):
        patcher, _ = _fake_async_client(resp=_response(500))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🟡", 500, None)

    async def test_connect_error_is_red_no_code(self):
        patcher, _ = _fake_async_client(exc=httpx.ConnectError("boom"))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🔴", None, "ConnectError")

    async def test_timeout_is_red_no_code(self):
        patcher, _ = _fake_async_client(exc=httpx.TimeoutException("too slow"))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        assert (emoji, code, err) == ("🔴", None, "TimeoutException")

    async def test_uses_active_chat_key_and_base_url(self):
        # The chat-set key must be sent; env key would report the wrong verdict.
        assert get_llm_api_key() == "env-key-1234567890abcdef"
        set_llm_api_key("sk-chat-key-abcdefgh12345678")
        patcher, get_mock = _fake_async_client(resp=_response(200))
        with patcher:
            emoji, code, err = await _ping_llm_api()
        headers = get_mock.await_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-chat-key-abcdefgh12345678"
        assert get_mock.await_args.args[0] == f"{settings.llm_base_url}/models"
        assert (emoji, code) == ("🟢", 200)
