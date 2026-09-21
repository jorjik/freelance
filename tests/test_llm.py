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
    llm._spent_tokens.clear()
    yield
    llm._consecutive_failures = 0
    llm._last_failure_notify = 0.0
    llm._quota_cooldown_until = 0.0
    llm._notify_callback = None
    llm._spent_tokens.clear()


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

    def test_read_error_is_network_error(self):
        # Регрессия: ReadError раньше попадал в общий str(exc) — админу
        # уходило непонятное «Ошибка LLM API: ReadError».
        text = _format_llm_error(httpx.ReadError("connection dropped"))
        assert "сетевая ошибка" in text
        assert "ReadError" in text

    def test_timeout_message(self):
        text = _format_llm_error(httpx.ReadTimeout("too slow"))
        assert "таймаут" in text

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

    async def test_retries_on_read_error_then_succeeds(self):
        # Регрессия: обрыв соединения при чтении ответа (ReadError) раньше
        # не ловился ретрай-логикой и срывал фильтрацию поста.
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        post = AsyncMock(side_effect=[httpx.ReadError("connection dropped"), ok])
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry({"messages": []})
        assert result == {"ok": True}
        assert post.await_count == 2
        sleep_mock.assert_awaited()

    async def test_exhausts_retries_on_read_error(self):
        post = AsyncMock(side_effect=httpx.ReadError("connection dropped"))
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(httpx.ReadError):
                await _call_llm_with_retry({"messages": []}, max_retries=2)
        assert post.await_count == 3  # 1 + 2 retries

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


def _rate_limit_error(retry_after: str | None = None, message: str | None = None):
    """httpx-ошибка 429 в форме, в которой её отдаёт Groq."""
    response = MagicMock()
    response.status_code = 429
    response.headers = {} if retry_after is None else {"retry-after": retry_after}
    if message is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = {"error": {"message": message}}
    return httpx.HTTPStatusError("Client error '429'", request=MagicMock(), response=response)


class TestRateLimitHandling:
    async def test_429_uses_retry_after_header(self):
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        post = AsyncMock(side_effect=[_rate_limit_error(retry_after="20"), ok])
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry({"messages": []})
        assert result == {"ok": True}
        assert sleep_mock.await_args.args[0] == 20.0

    async def test_429_without_header_waits_for_window_not_one_second(self):
        # Регрессия: бэкофф 1/2/4 с не освобождает минутное окно TPM,
        # поэтому пост терялся после четвёртой попытки.
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        post = AsyncMock(side_effect=[_rate_limit_error(), _rate_limit_error(), ok])
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await _call_llm_with_retry({"messages": []})
        waits = [call.args[0] for call in sleep_mock.await_args_list]
        assert waits and all(wait >= llm.RATE_LIMIT_WAIT_SECONDS for wait in waits)

    async def test_429_message_is_human_readable(self):
        error = _rate_limit_error(message="Rate limit reached for model on tokens per minute")
        text = _format_llm_error(error)
        assert "лимит провайдера исчерпан" in text
        assert "tokens per minute" in text
        assert "for url" not in text

    def test_retry_after_is_capped_and_ignores_garbage(self):
        assert llm._retry_after_seconds(
            _rate_limit_error(retry_after="99999").response
        ) == llm.MAX_RETRY_AFTER_SECONDS
        assert llm._retry_after_seconds(_rate_limit_error(retry_after="55").response) == 55.0
        assert llm._retry_after_seconds(_rate_limit_error(retry_after="soon").response) is None
        assert llm._retry_after_seconds(_rate_limit_error().response) is None

    async def test_call_waits_when_minute_budget_is_spent(self):
        payload = {
            "messages": [{"content": "x" * 3000}],
            "max_tokens": llm.MAX_TOKENS_JUDGE,
        }
        llm._spent_tokens.append((time.monotonic(), llm.TPM_BUDGET))
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            llm._spent_tokens.clear()  # окно освободилось

        result_payload = {"choices": [{"message": {"content": "{}"}}]}
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = result_payload
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=fake_sleep
        ):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=response)))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry(payload)
        assert result == result_payload
        assert sleeps and sleeps[0] >= 1.0

    async def test_daily_limit_429_pauses_filtering_without_retries(self):
        # Регрессия: суточный лимит не освобождается за минуту, а бот
        # молотил 4 попытки по 60 с и терял посты один за другим.
        error = _rate_limit_error(
            retry_after="3600",
            message="Rate limit reached for model on tokens per day (TPD): "
            "Limit 200000, Used 199752. Please try again in 3599s.",
        )
        callback = AsyncMock()
        llm.set_notify_callback(callback)
        post = AsyncMock(side_effect=error)
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ):
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(QuotaPausedError):
                await _call_llm_with_retry({"messages": []})
        assert post.await_count == 1  # ни ожидания, ни повторов
        assert is_quota_paused()
        assert "Суточный лимит" in callback.await_args.args[0]

    async def test_daily_limit_short_wait_is_retried(self):
        # Окно скользящее: если провайдер просит подождать минуту, ждём
        # и повторяем — пост проходит.
        error = _rate_limit_error(
            retry_after="55",
            message="Rate limit reached for model on tokens per day (TPD): "
            "Limit 200000, Used 199752. Please try again in 55s.",
        )
        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {"ok": True}
        post = AsyncMock(side_effect=[error, ok])
        with patch("src.llm.httpx.AsyncClient") as client_cls, patch(
            "src.llm.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=post))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await _call_llm_with_retry({"messages": []})
        assert result == {"ok": True}
        assert sleep_mock.await_args.args[0] == 55.0
        assert not is_quota_paused()

    def test_daily_limit_detection(self):
        assert llm._is_daily_limit("... on tokens per day (TPD): Limit 200000")
        assert not llm._is_daily_limit("... on tokens per minute (TPM): Limit 8000")

    async def test_usage_is_remembered_for_pacing(self):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"total_tokens": 1234},
        }
        with patch("src.llm.httpx.AsyncClient") as client_cls:
            client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=response)))
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await _call_llm_with_retry({"messages": []})
        assert [tokens for _, tokens in llm._spent_tokens] == [1234]


class TestRequestBudget:
    async def test_judge_payload_caps_response_and_reasoning(self):
        payload = {"choices": [{"message": {"content": '{"decision": "REJECT"}'}}]}
        call = AsyncMock(return_value=payload)
        with patch("src.llm._call_llm_with_retry", new=call):
            await judge_vacancy("vacancy text", {})
        sent = call.await_args.args[0]
        assert sent["max_tokens"] == llm.MAX_TOKENS_JUDGE
        if "groq" in llm.settings.llm_base_url:
            assert sent["reasoning_effort"] == llm.REASONING_EFFORT

    async def test_response_payload_caps_response(self):
        payload = {"choices": [{"message": {"content": "текст отклика"}}]}
        call = AsyncMock(return_value=payload)
        with patch("src.llm._call_llm_with_retry", new=call):
            await llm.generate_response_text("vacancy text")
        assert call.await_args.args[0]["max_tokens"] == llm.MAX_TOKENS_RESPONSE

    def test_reasoning_effort_is_not_sent_to_other_providers(self):
        with patch.object(llm.settings, "llm_base_url", "https://api.openai.com/v1"):
            assert llm._reasoning_params() == {}
        with patch.object(llm.settings, "llm_base_url", "https://api.groq.com/openai/v1"):
            assert llm._reasoning_params() == {"reasoning_effort": llm.REASONING_EFFORT}
