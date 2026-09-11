"""Tests for parse_channel_input — pure logic, no network, no .env needed.

Run:  python -m pytest tests/ -v
"""
import os
import sys
from pathlib import Path

# Provide dummy env vars before importing src.config (pydantic-settings
# requires all mandatory fields at import time; tests don't use them).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "1")
os.environ.setdefault("LLM_API_KEY", "test-key")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.bot import parse_channel_input


class TestParseChannelInputValid:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("@FreelancehuntProjects", "@FreelancehuntProjects"),
            ("FreelancehuntProjects", "@FreelancehuntProjects"),
            ("https://t.me/FreelancehuntProjects", "@FreelancehuntProjects"),
            ("http://t.me/FreelancehuntProjects", "@FreelancehuntProjects"),
            ("https://www.t.me/FreelancehuntProjects", "@FreelancehuntProjects"),
            ("https://telegram.me/FreelancehuntProjects", "@FreelancehuntProjects"),
            ("https://t.me/FreelancehuntProjects/42", "@FreelancehuntProjects"),
            ("  @some_user  ", "@some_user"),
            ("https://t.me/some_user", "@some_user"),
        ],
    )
    def test_valid_inputs(self, raw, expected):
        assert parse_channel_input(raw) == expected

    def test_takes_first_token_only(self):
        assert parse_channel_input("https://t.me/food bar") == "@food"


class TestParseChannelInputInvalid:
    def test_empty_string(self):
        with pytest.raises(ValueError, match="Пустое значение"):
            parse_channel_input("")
        with pytest.raises(ValueError, match="Пустое значение"):
            parse_channel_input("   ")

    @pytest.mark.parametrize(
        "raw",
        [
            "https://t.me/+abc123",
            "+abc123",
            "https://t.me/joinchat/abc",
            "joinchat/abc",
        ],
    )
    def test_private_invite_links_rejected(self, raw):
        with pytest.raises(ValueError, match="Приватные invite-ссылки"):
            parse_channel_input(raw)

    @pytest.mark.parametrize(
        "raw",
        ["https://t.me/addstickers", "https://t.me/iv"],
    )
    def test_reserved_paths_rejected(self, raw):
        with pytest.raises(ValueError, match="Это не ссылка на канал"):
            parse_channel_input(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "ab",  # too short (< 4)
            "a" * 33,  # too long (> 32)
            "has space",
            "bad!name",
            "кириллица",
            "https://example.com/foo",
            "https://t.me/",
        ],
    )
    def test_bad_usernames_rejected(self, raw):
        with pytest.raises(ValueError, match="Не понял канал|Некорректный username"):
            parse_channel_input(raw)


class TestParseChannelInputEdgeCases:
    def test_username_min_length(self):
        assert parse_channel_input("abcd") == "@abcd"

    def test_username_max_length(self):
        name = "a" * 32
        assert parse_channel_input(name) == f"@{name}"

    def test_case_insensitive_domain(self):
        assert parse_channel_input("HTTPS://T.ME/Food") == "@Food"

    def test_joinchat_link_hits_invite_branch_first(self):
        # "joinchat" is matched by the private-invite check before
        # the reserved-paths check — same rejection either way.
        with pytest.raises(ValueError, match="Приватные invite-ссылки"):
            parse_channel_input("https://t.me/joinchat")
