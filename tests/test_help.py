"""Tests for /help and /start output: every registered command must be
listed in HELP_TEXT (checked against build_bot's actual route table).
"""
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("LLM_API_KEY", "test-key")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import (
    HELP_TEXT,
    MENU_HELP,
    _main_keyboard,
    help_command,
    settings_text_input,
    start,
)


def _update_and_context():
    update = MagicMock()
    update.effective_user.id = 1  # matches ADMIN_TELEGRAM_ID
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    return update, context


class TestHelpText:
    def test_lists_every_registered_command(self):
        # Список синхронизирован с маршруты build_bot (CommandHandler в src/bot.py).
        expected = {
            "start", "help", "add_channel", "channels", "status",
            "set_key", "forget_key", "set_model", "forget_model",
        }
        for name in expected:
            assert f"/{name}" in HELP_TEXT, f"/{name} отсутствует в справке"

    def test_every_line_with_command_is_real_command(self):
        import re
        # Команда стоит в начале строки; «/» внутри примеров (openai/gpt-oss-20b)
        # командой не является.
        for match in re.findall(r"^/([a-z_]+)", HELP_TEXT, re.MULTILINE):
            assert match in {
                "start", "help", "add_channel", "channels", "status",
                "set_key", "forget_key", "set_model", "forget_model",
            }, f"в справке упомянута несуществующая команда /{match}"


class TestHelpCommand:
    async def test_replies_with_help_text(self):
        update, context = _update_and_context()
        await help_command(update, context)
        update.message.reply_text.assert_awaited_once_with(HELP_TEXT)

    async def test_ignores_non_admin(self):
        update, context = _update_and_context()
        update.effective_user.id = 999
        await help_command(update, context)
        update.message.reply_text.assert_not_awaited()


class TestStart:
    async def test_start_includes_help_text(self):
        update, context = _update_and_context()
        update.effective_message.reply_text = AsyncMock()
        await start(update, context)
        kwargs = update.effective_message.reply_text.await_args.kwargs
        text = update.effective_message.reply_text.await_args.args[0]
        assert HELP_TEXT in text
        assert "reply_markup" in kwargs


class TestHelpButton:
    """Постоянная кнопка справки в меню внизу чата."""

    def test_three_buttons_in_one_row(self):
        keyboard = _main_keyboard()
        assert len(keyboard.keyboard) == 1
        assert [button.text for button in keyboard.keyboard[0]] == [
            "Парсинг",
            "Настройка",
            "?",
        ]
        assert keyboard.keyboard[0][-1].text == MENU_HELP

    def test_keyboard_stays_persistent(self):
        keyboard = _main_keyboard()
        assert keyboard.is_persistent is True
        assert keyboard.resize_keyboard is True

    async def test_button_text_replies_with_help(self):
        # Кнопка «?» приходит обычным текстом, не командой.
        update, context = _update_and_context()
        update.message.text = MENU_HELP
        await settings_text_input(update, context)
        update.message.reply_text.assert_awaited_once_with(HELP_TEXT)
