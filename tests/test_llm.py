"""Tests for LLM retry logic, quota cooldown and error formatting.

All HTTP calls are mocked — no network access.
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("LLM_API_KEY", "test-key")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import pytest

import src.llm as llm
from src.llm import (
    QUOTA_COOLDOWN_SECONDS,
    QuotaPausedError,
    _call_llm_with_retry,
    _format_llm_error,
    _is_quota_error,
    is_quota_paused,
    judge_vacancy,
    normalize_decision,
    quota_cooldown_remaining,
)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    response = MagicMock()
    response.status_code = status
    request = MagicMock()
    return httpx.HTTPStatusError(
        f"Client error '{status}'", request=request, response=response
    )


@pytest.fixture(autouse=True)
def _reset_llm_state():
    llm._consecutive_failures = 0
    llm._last_failure_notify = 0.0
    llm._quota_cooldown_until = 0.0
    llm._notify_callback = None
    yield
    llm._consecutive_failures = 0
    llm._last_failure_notify = 0.0
    llm._quota_cooldown_until = 0.0
    llm._notify_callback = None


class TestNormalizeDecision:
    def test_valid_decisions(self):
        assert normalize_decision("ACCEPT") == "ACCEPT"
        assert normalize_decision("reject") == "REJECT"
        assert normalize_decision(" Review ") == "REVIEW"

    def test_invalid_falls_back_to_review(self):
        assert normalize_decision("MAYBE") == "REVIEW"
        assert normalize_decision("") == "REVIEW"
        assert normalize_decision(None) == "REVIEW"


class TestFormatLlmError:
    def test_402_message(self):
        assert "баланс/токены LLM исчерпаны" in _format_llm_error(_http_status_error(402))

    def test_401_message(self):
        assert "проверь LLM API ключ" in _format_llm_error(_http_status_error(401))

    def test_other_http_status(self):
        text = _format_llm_error(_http_status_error(500))
        assert "HTTP 500" in text

    def test_non_http_exception(self):
        assert "boom" in _format_llm_error(ValueError("boom"))
        assert _format_llm_error(ValueError()) == "ValueError"


class TestIsQuotaError:
    def test_402_is_quota(self):
        assert _is_quota_error(_http_status_error(402)) is True

    @pytest.mark.parametrize("status", [400, 401, 429, 500])
    def test_other_statuses_not_quota(self, status):
        assert _is_quota_error(_http_status_error(status)) is False

    def test_non_http_exception_not_quota(self):
        assert _is_quota_error(ValueError("x")) is False


class TestQuotaCooldown:
    async def test_notify_llm_error_arms_cooldown_and_forces_notify(self):
        callback = AsyncMock()
        llm.set_notify_callback(callback)
        await llm.notify_llm_error(_http_status_error(402))
        callback.assert_awaited_once()
        message = callback.await_args.args[0]
        assert "Баланс/токены LLM исчерпаны" in message
        assert is_quota_paused()
        assert 0 < quota_cooldown_remaining() <= QUOTA_COOLDOWN_SECONDS

    async def test_second_402_during_cooldown_is_silent(self):
        callback = AsyncMock()
        llm.set_notify_callback(callback)
        await llm.notify_llm_error(_http_status_error(402))
        await llm.notify_llm_error(_http_status_error(402))
        await llm.notify_llm_error(_http_status_error(402))
        assert callback.await_count == 1  # only the first 402 notifies

    async def test_non_quota_error_uses_throttle(self):
        callback = AsyncMock()
        llm.set_notify_callback(callback)
        await llm.notify_llm_error(ValueError("x"))
        callback.assert_awaited_once()
        assert not is_quota_paused()

    async def test_cooldown_expires(self):
        llm._quota_cooldown_until = time.time() - 1
        assert not is_quota_paused()
        assert quota_cooldown_remaining() == 0.0


class TestCallLlmWithRetry:
    async def test_success_first_try(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        with patch("src.llm.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=response)))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry({"messages": []})
        assert result["choices"][0]["message"]["content"] == "{}"

    async def test_no_retry_on_402(self):
        post = AsyncMock(side_effect=_http_status_error(402))
        with patch("src.llm.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                await _call_llm_with_retry({"messages": []})
        assert post.await_count == 1  # no retries

    async def test_no_retry_on_401(self):
        post = AsyncMock(side_effect=_http_status_error(401))
        with patch("src.llm.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                await _call_llm_with_retry({"messages": []})
        assert post.await_count == 1

    async def test_retries_on_429_then_succeeds(self):
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        post = AsyncMock(side_effect=[_http_status_error(429), ok])
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry({"messages": []})
        assert result == {"ok": True}
        assert post.await_count == 2
        sleep_mock.assert_awaited()

    async def test_exhausts_retries_on_500(self):
        post = AsyncMock(side_effect=_http_status_error(500))
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                await _call_llm_with_retry({"messages": []}, max_retries=2)
        assert post.await_count == 3  # 1 + 2 retries


class TestJudgeVacancyQuotaPause:
    async def test_judge_raises_when_paused(self):
        llm._quota_cooldown_until = time.time() + 60
        with pytest.raises(QuotaPausedError):
            await judge_vacancy("text", {})


class TestJudgeVacancyParsing:
    async def test_parses_valid_json(self):
        payload = {"choices": [{"message": {"content": '{"decision": "ACCEPT", "reason": "ok", "price_estimate": 100}'}}]}
        with patch("src.llm._call_llm_with_retry", new=AsyncMock(return_value=payload)):
            result = await judge_vacancy("vacancy text", {"filter_stack": "Python"})
        assert result["decision"] == "ACCEPT"
        assert result["reason"] == "ok"
        assert result["price_estimate"] == 100

    async def test_bad_json_falls_back_to_review(self):
        payload = {"choices": [{"message": {"content": "not json"}}]}
        with patch("src.llm._call_llm_with_retry", new=AsyncMock(return_value=payload)):
            result = await judge_vacancy("vacancy text", {})
        assert result["decision"] == "REVIEW"
        assert result["reason"] == "Failed to parse LLM response"
