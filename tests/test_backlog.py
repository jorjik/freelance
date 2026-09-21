"""Тесты догона постов, пропущенных из-за лимита LLM.

Пост, который не оценили при 429, остаётся в базе без решения и админу не
показывается. Догон переоценивает такие посты небольшими пачками.
HTTP и база замоканы — сеть не нужна.
"""
import os
import sqlite3
import sys
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

from src import db as db_module
from src.bot import _format_accepted_message
from src.config import settings
from src.llm import QuotaPausedError
from src.parser import BACKLOG_MAX_AGE_HOURS, Parser


@pytest.fixture(autouse=True)
def _workdir(tmp_path, monkeypatch):
    """Parser создаёт ./sessions и ./downloads — уводим их в tmp."""
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def parser():
    return Parser()


def _pending(vacancy_id: int, decision: str = "ACCEPT"):
    return {
        "id": vacancy_id,
        "message_id": 1000 + vacancy_id,
        "text": f"пост {vacancy_id}: нужен разработчик сайта",
        "username": "@FreelancehuntProjects",
    }


def _judge_result(decision: str):
    return {"decision": decision, "reason": "тест", "price_estimate": None}


@pytest.fixture
def mocks():
    """Общий набор подмен: база, правила, суждение."""
    with patch("src.parser.list_pending_vacancies", new=AsyncMock(return_value=[])) as pending, \
        patch("src.parser.are_filter_settings_complete", new=AsyncMock(return_value=True)), \
        patch("src.parser.get_filter_settings", new=AsyncMock(return_value={"filter_take": "сайты"})), \
        patch("src.parser.update_vacancy_ai", new=AsyncMock()) as update, \
        patch("src.parser.judge_vacancy", new=AsyncMock()) as judge, \
        patch("src.parser.is_quota_paused", return_value=False) as paused, \
        patch("src.parser.notify_llm_error", new=AsyncMock()) as notify:
        yield {
            "pending": pending,
            "update": update,
            "judge": judge,
            "paused": paused,
            "notify": notify,
        }


class TestRefilterPending:
    async def test_accepted_post_is_sent_to_admin_marked_restored(self, parser, mocks):
        mocks["pending"].return_value = [_pending(11), _pending(12)]
        mocks["judge"].side_effect = [_judge_result("ACCEPT"), _judge_result("REJECT")]
        sent = AsyncMock()
        parser._on_accepted = sent

        processed = await parser.refilter_pending()

        assert processed == 2
        assert mocks["update"].await_count == 2
        sent.assert_awaited_once()
        post = sent.await_args.args[0]
        assert post["id"] == 11
        assert post["restored"] is True
        assert post["link"] == "https://t.me/FreelancehuntProjects/1011"
        assert post["channel"] == "@FreelancehuntProjects"

    async def test_rejected_post_is_not_sent(self, parser, mocks):
        mocks["pending"].return_value = [_pending(21)]
        mocks["judge"].return_value = _judge_result("REJECT")
        sent = AsyncMock()
        parser._on_accepted = sent

        await parser.refilter_pending()

        sent.assert_not_awaited()
        assert mocks["update"].await_args.args[1] == "REJECT"

    async def test_does_nothing_while_quota_paused(self, parser, mocks):
        mocks["paused"].return_value = True

        assert await parser.refilter_pending() == 0
        mocks["pending"].assert_not_awaited()
        mocks["judge"].assert_not_awaited()

    async def test_stops_when_provider_limit_hits_midway(self, parser, mocks):
        mocks["pending"].return_value = [_pending(31), _pending(32)]
        mocks["judge"].side_effect = QuotaPausedError("суточный лимит")
        sent = AsyncMock()
        parser._on_accepted = sent

        assert await parser.refilter_pending() == 0
        mocks["update"].assert_not_awaited()
        sent.assert_not_awaited()
        # Неизвестная ошибка API — это отдельная ветка: админа уведомляют
        # и догон останавливается, чтобы не долбить мёртвый эндпоинт.
        mocks["notify"].assert_not_awaited()

    async def test_reports_api_failure_and_stops(self, parser, mocks):
        mocks["pending"].return_value = [_pending(41), _pending(42)]
        mocks["judge"].side_effect = RuntimeError("boom")

        assert await parser.refilter_pending() == 0
        mocks["notify"].assert_awaited_once()
        assert mocks["judge"].await_count == 1

    async def test_bad_post_is_skipped_and_batch_continues(self, parser, mocks):
        # Реальный случай: qwen на одном посте вернул 400 json_validate_failed.
        # Это ошибка поста, а не эндпоинта — остальные посты должны пройти.
        bad_response = MagicMock()
        bad_response.status_code = 400
        bad_post = httpx.HTTPStatusError(
            "400 Bad Request", request=MagicMock(), response=bad_response
        )
        mocks["pending"].return_value = [_pending(61), _pending(62)]
        mocks["judge"].side_effect = [bad_post, _judge_result("REJECT")]

        processed = await parser.refilter_pending()

        assert processed == 1
        assert mocks["judge"].await_count == 2
        mocks["update"].assert_awaited_once()
        # Пост-специфичная ошибка не должна дёргать админа.
        mocks["notify"].assert_not_awaited()

    async def test_batch_is_bounded_and_recent(self, parser, mocks):
        mocks["pending"].return_value = [_pending(51)]
        mocks["judge"].return_value = _judge_result("REJECT")

        await parser.refilter_pending()

        kwargs = mocks["pending"].await_args.kwargs
        assert kwargs["max_age_hours"] == BACKLOG_MAX_AGE_HOURS
        assert kwargs["limit"] <= 20  # пачками, а не всей базой

    async def test_nothing_pending_is_noop(self, parser, mocks):
        assert await parser.refilter_pending() == 0
        mocks["judge"].assert_not_awaited()

    async def test_skips_when_filter_rules_are_empty(self, parser, mocks):
        with patch("src.parser.are_filter_settings_complete", new=AsyncMock(return_value=False)):
            assert await parser.refilter_pending() == 0
        mocks["pending"].assert_not_awaited()


class TestAcceptedMessageMark:
    def test_restored_post_is_marked(self):
        text, _keyboard = _format_accepted_message(
            {"text": "пост", "link": "https://t.me/x/1", "id": 5, "restored": True}
        )
        assert "из догона" in text

    def test_live_post_is_not_marked(self):
        text, _keyboard = _format_accepted_message(
            {"text": "пост", "link": "https://t.me/x/1", "id": 5}
        )
        assert "из догона" not in text


class TestPendingVacanciesQuery:
    """Реальная база в tmp: догон не должен тянуть старые и уже решённые посты."""

    @pytest.fixture(autouse=True)
    def _temp_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "database_path", str(tmp_path / "pending.db"))
        yield

    async def _seed(self) -> None:
        await db_module.init_db()
        channel_id = await db_module.add_channel("@test_channel", "test")
        await db_module.upsert_vacancy(channel_id, 1, "свежий без решения")
        await db_module.upsert_vacancy(channel_id, 2, "свежий с решением")
        await db_module.upsert_vacancy(channel_id, 3, "старый без решения")
        decided_id, _ = await db_module.upsert_vacancy(channel_id, 2, "свежий с решением")
        await db_module.update_vacancy_ai(decided_id, "REJECT", "не подходит")
        conn = sqlite3.connect(settings.database_path)
        conn.execute(
            "UPDATE vacancies SET created_at = datetime('now', '-3 days') WHERE message_id = 3"
        )
        conn.commit()
        conn.close()

    async def test_returns_only_recent_undecided(self):
        await self._seed()

        rows = await db_module.list_pending_vacancies(limit=10, max_age_hours=24)

        assert [row["message_id"] for row in rows] == [1]
        assert rows[0]["username"] == "@test_channel"

    async def test_limit_is_applied(self):
        await self._seed()

        assert len(await db_module.list_pending_vacancies(limit=1, max_age_hours=720)) == 1
