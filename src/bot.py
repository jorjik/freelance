import asyncio
import logging
import re
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CopyTextButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config import settings
from src.models import Mode
from src.db import (
    get_ai_vacancies,
    get_vacancy_by_id,
    get_channel_by_id,
    add_response,
    set_response_sent,
    get_channels,
    add_channel,
    get_filter_settings,
    set_setting,
    are_filter_settings_complete,
    FILTER_KEYS,
    get_parse_mode,
    set_parse_mode,
    PARSE_MODE_REALTIME,
    PARSE_MODE_BATCH,
)
from src.llm import generate_response_text, set_notify_callback
from src.parser import Parser


logger = logging.getLogger(__name__)
_parser: Optional[Parser] = None
_app: Optional[Application] = None
_batch_running = False
COPY_TEXT_MAX = 256

CHANNEL_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(@?[A-Za-z0-9_]+)(?:/\d+)?/?$",
    re.IGNORECASE,
)
CHANNEL_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
RESERVED_TG_PATHS = {
    "joinchat",
    "addstickers",
    "share",
    "proxy",
    "socks",
    "iv",
    "login",
    "s",
    "c",
}


def _clear_awaiting(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    context.user_data.pop("awaiting_setting", None)
    context.user_data.pop("awaiting_channel", None)


def parse_channel_input(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустое значение. Пришли ссылку или никнейм.")
    text = text.split()[0].strip()
    if text.startswith("+") or "/+" in text or "joinchat" in text.lower():
        raise ValueError(
            "Приватные invite-ссылки не поддерживаются. "
            "Пришли публичный @username или https://t.me/username"
        )
    match = CHANNEL_LINK_RE.match(text)
    if match:
        name = match.group(1).lstrip("@")
        if name.lower() in RESERVED_TG_PATHS:
            raise ValueError(
                "Это не ссылка на канал. Пример: https://t.me/FreelancehuntProjects"
            )
        if not CHANNEL_USERNAME_RE.fullmatch(name):
            raise ValueError("Некорректный username в ссылке.")
        return f"@{name}"
    name = text[1:] if text.startswith("@") else text
    if not CHANNEL_USERNAME_RE.fullmatch(name):
        raise ValueError(
            "Не понял канал.\n"
            "Примеры:\n"
            "@FreelancehuntProjects\n"
            "FreelancehuntProjects\n"
            "https://t.me/FreelancehuntProjects"
        )
    return f"@{name}"


async def _save_channel_and_reply(message, username: str) -> None:
    channel_id = await add_channel(username, title=username)
    note = ""
    if await get_parse_mode() == PARSE_MODE_REALTIME:
        try:
            parser = await _get_parser()
            if parser.is_listening:
                await parser.stop_realtime()
            note = "\n" + await _ensure_realtime(start=True)
        except Exception as exc:
            note = f"\nRealtime не перезапущен: {exc}"
    await message.reply_text(f"Канал сохранен: {username}\nID: {channel_id}{note}")


def _response_actions_keyboard(vacancy_id: int, reply_text: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Bot API CopyTextButton accepts at most 256 chars; longer texts are
    # sent as a plain message so the user can long-press → Copy.
    if 1 <= len(reply_text) <= COPY_TEXT_MAX:
        rows.append(
            [
                InlineKeyboardButton(
                    "Скопировать",
                    copy_text=CopyTextButton(text=reply_text),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("Да", callback_data=f"response:confirm:{vacancy_id}:1"),
            InlineKeyboardButton("Нет", callback_data=f"response:confirm:{vacancy_id}:0"),
        ]
    )
    return InlineKeyboardMarkup(rows)

FILTER_LABELS = {
    "filter_stack": "Стек / навыки",
    "filter_budget_min": "Мин. бюджет",
    "filter_budget_max": "Макс. бюджет (0 = без лимита)",
    "filter_take": "Что брать",
    "filter_skip": "Что не брать",
    "filter_notes": "Доп. указания для ИИ",
}

FILTER_HINTS = {
    "filter_stack": "Например: Python, Django, Telegram bots",
    "filter_budget_min": "Число, например: 200",
    "filter_budget_max": "Число, например: 5000. 0 = без верхнего лимита",
    "filter_take": "Например: Долгосрок, backend, API",
    "filter_skip": "Например: Верстка, 1С, срочно за еду",
    "filter_notes": "Любые доп. правила для ИИ",
}


async def _get_parser() -> Parser:
    global _parser
    if _parser is None:
        _parser = Parser()
    return _parser


def _only_admin(user_id: int) -> bool:
    return user_id == settings.admin_telegram_id


MENU_START = "Парсинг"
MENU_SETTINGS = "Настройка"


def _main_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню внизу чата."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(MENU_START), KeyboardButton(MENU_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _format_accepted_message(post: dict) -> tuple[str, InlineKeyboardMarkup]:
    body = (post.get("text") or "").strip()
    link = (post.get("link") or "").strip()
    suffix = f"\n\nИсходный пост: {link}" if link else ""
    max_body = 4000 - len(suffix)
    if len(body) > max_body:
        body = body[: max(0, max_body - 3)] + "..."
    msg = body + suffix
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("сгенерировать ответ", callback_data=f"response:generate:{post.get('id')}")]]
    )
    return msg, keyboard


async def _notify_accepted(post: dict) -> None:
    if _app is None:
        return
    msg, keyboard = _format_accepted_message(post)
    await _app.bot.send_message(
        chat_id=settings.admin_telegram_id,
        text=msg,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _ensure_realtime(start: bool = True) -> str:
    parser = await _get_parser()
    mode = await get_parse_mode()
    if mode != PARSE_MODE_REALTIME:
        if parser.is_listening:
            await parser.stop_realtime()
        return "Режим разовой выборки — realtime выключен"
    if not await are_filter_settings_complete():
        if parser.is_listening:
            await parser.stop_realtime()
        return "Сначала заполни фильтр ИИ"
    if not start:
        if parser.is_listening:
            await parser.stop_realtime()
        return "Realtime остановлен"
    if parser.is_listening:
        return "Realtime уже слушает: " + ", ".join(parser.watch_usernames)
    await parser.start_realtime(_notify_accepted)
    return "Realtime запущен: " + ", ".join(parser.watch_usernames)


async def _settings_keyboard() -> InlineKeyboardMarkup:
    mode = await get_parse_mode()
    mode_label = (
        "Режим: в реальном времени"
        if mode == PARSE_MODE_REALTIME
        else "Режим: разовая выборка"
    )
    rows = [[InlineKeyboardButton(mode_label, callback_data="settings:toggle_mode")]]
    data = await get_filter_settings()
    for key in FILTER_KEYS:
        value = (data.get(key) or "").strip()
        mark = "✓" if value else "○"
        preview = value if len(value) <= 28 else value[:25] + "..."
        label = f"{mark} {FILTER_LABELS[key]}"
        if preview:
            label += f": {preview}"
        rows.append([InlineKeyboardButton(label, callback_data=f"settings:edit:{key}")])
    rows.append([InlineKeyboardButton("Назад", callback_data="back:menu")])
    return InlineKeyboardMarkup(rows)


async def _settings_status_text() -> str:
    mode = await get_parse_mode()
    mode_ru = "в реальном времени" if mode == PARSE_MODE_REALTIME else "разовая выборка"
    data = await get_filter_settings()
    filled = sum(1 for k in FILTER_KEYS if (data.get(k) or "").strip())
    total = len(FILTER_KEYS)
    status = "готово к парсингу" if filled >= 1 else "парсинг заблокирован"
    return (
        f"Настройки парсинга\n"
        f"Режим: {mode_ru}\n"
        f"Фильтр ИИ: {filled}/{total} — {status}\n"
        f"Достаточно минимум 1 поля фильтра.\n\n"
        f"Нажми режим, чтобы переключить. Нажми поле фильтра, чтобы изменить."
    )


async def _realtime_status_text() -> str:
    parser = await _get_parser()
    channels = await get_channels(limit=50)
    names = [c.get("username") or c.get("title") or str(c.get("id")) for c in channels]
    status = "слушает" if parser.is_listening else "остановлен"
    watched = ", ".join(parser.watch_usernames) if parser.watch_usernames else "—"
    ch_list = ", ".join(names) if names else "нет (добавь /add_channel)"
    return (
        f"Парсинг в реальном времени\n"
        f"Статус: {status}\n"
        f"Слушаю: {watched}\n"
        f"Каналы в базе: {ch_list}"
    )


async def _realtime_keyboard() -> InlineKeyboardMarkup:
    parser = await _get_parser()
    rows = []
    if parser.is_listening:
        rows.append([InlineKeyboardButton("Остановить", callback_data="parser:live:stop")])
    else:
        rows.append([InlineKeyboardButton("Запустить", callback_data="parser:live:start")])
    rows.append([InlineKeyboardButton("Назад", callback_data="back:menu")])
    return InlineKeyboardMarkup(rows)


async def _open_parser(message) -> None:
    if not await are_filter_settings_complete():
        await message.reply_text(
            "Сначала заполни Настройки фильтра ИИ.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Открыть настройки", callback_data="settings:menu")]]
            ),
        )
        return
    parse_mode = await get_parse_mode()
    if parse_mode == PARSE_MODE_REALTIME:
        await message.reply_text(
            await _realtime_status_text(),
            reply_markup=await _realtime_keyboard(),
        )
        return
    channels = await get_channels(limit=50)
    if not channels:
        await message.reply_text("Каналов нет. Добавь через /add_channel")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                c["username"] or c["title"] or str(c["id"]),
                callback_data=f"parser:channel:{c['id']}",
            )
        ]
        for c in channels
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="back:menu")])
    await message.reply_text(
        "Разовая выборка: выбери канал",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _open_settings(message) -> None:
    await message.reply_text(
        await _settings_status_text(),
        reply_markup=await _settings_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _only_admin(update.effective_user.id):
        return
    _clear_awaiting(context)
    target = update.effective_message
    if target is None:
        return
    await target.reply_text(
        "Freelance parser\nВыбери действие в меню внизу.",
        reply_markup=_main_keyboard(),
    )


async def add_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _only_admin(update.effective_user.id):
        return
    if context.user_data is None:
        return
    # Optional shortcut: /add_channel @username
    if context.args:
        try:
            username = parse_channel_input(" ".join(context.args))
            context.user_data.pop("awaiting_channel", None)
            context.user_data.pop("awaiting_setting", None)
            await _save_channel_and_reply(update.message, username)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
        except Exception as exc:
            logger.exception("Add channel failed")
            await update.message.reply_text(f"Ошибка: {exc}")
        return

    context.user_data.pop("awaiting_setting", None)
    context.user_data["awaiting_channel"] = True
    await update.message.reply_text(
        "Пришли ссылку на канал или никнейм одним сообщением.\n\n"
        "Примеры:\n"
        "@FreelancehuntProjects\n"
        "FreelancehuntProjects\n"
        "https://t.me/FreelancehuntProjects",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="back:menu")]]
        ),
    )


async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _only_admin(update.effective_user.id):
        return
    channels = await get_channels(limit=50)
    if not channels:
        await update.message.reply_text("Каналов нет. Добавь через /add_channel")
        return
    lines = [f"{c['id']}. {c['username'] or c['title']}" for c in channels]
    await update.message.reply_text("Каналы:\n" + "\n".join(lines))


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return

    await query.answer()
    _clear_awaiting(context)
    mode = query.data.split(":")[1]
    if mode != Mode.PARSER and mode != Mode.RESPONSE:
        await query.message.reply_text("Неизвестный режим")
        return

    if mode == Mode.PARSER:
        await _open_parser(query.message)
    elif mode == Mode.RESPONSE:
        await _show_response_menu(query.message)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    _clear_awaiting(context)
    await _open_settings(query.message)


async def settings_toggle_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    current = await get_parse_mode()
    new_mode = PARSE_MODE_BATCH if current == PARSE_MODE_REALTIME else PARSE_MODE_REALTIME
    await set_parse_mode(new_mode)
    note = ""
    try:
        if new_mode == PARSE_MODE_REALTIME:
            note = "\n" + await _ensure_realtime(start=True)
        else:
            note = "\n" + await _ensure_realtime(start=False)
    except Exception as exc:
        note = f"\nНе удалось переключить realtime: {exc}"
    await query.message.reply_text(
        (await _settings_status_text()) + note,
        reply_markup=await _settings_keyboard(),
    )


async def settings_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    key = query.data.split(":", 2)[2]
    if key not in FILTER_KEYS:
        await query.message.reply_text("Неизвестное поле")
        return
    if context.user_data is not None:
        context.user_data.pop("awaiting_channel", None)
        context.user_data["awaiting_setting"] = key
    current = (await get_filter_settings()).get(key) or ""
    current_line = current if current.strip() else "(пусто)"
    await query.message.reply_text(
        f"{FILTER_LABELS[key]}\n"
        f"Сейчас: {current_line}\n\n"
        f"{FILTER_HINTS[key]}\n\n"
        f"Пришли новое значение одним сообщением.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Назад", callback_data="settings:menu")]]
        ),
    )


async def settings_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _only_admin(update.effective_user.id):
        return
    if not update.message:
        return
    text = (update.message.text or "").strip()
    user_data = context.user_data or {}

    # Постоянное меню — имеет приоритет над вводом значения
    if text in (MENU_START, MENU_SETTINGS):
        _clear_awaiting(context)
        if text == MENU_START:
            await _open_parser(update.message)
        else:
            await _open_settings(update.message)
        return

    if user_data.get("awaiting_channel"):
        try:
            username = parse_channel_input(text)
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return
        context.user_data.pop("awaiting_channel", None)
        try:
            await _save_channel_and_reply(update.message, username)
        except Exception as exc:
            logger.exception("Add channel failed")
            await update.message.reply_text(f"Ошибка: {exc}")
        return

    key = user_data.get("awaiting_setting")
    if not key:
        await update.message.reply_text(
            "Выбери действие в меню внизу.",
            reply_markup=_main_keyboard(),
        )
        return
    value = text
    if not value:
        await update.message.reply_text("Пустое значение. Пришли текст ещё раз.")
        return

    if key in ("filter_budget_min", "filter_budget_max"):
        try:
            num = float(value.replace(",", "."))
        except ValueError:
            await update.message.reply_text("Нужно число. Например: 200")
            return
        if key == "filter_budget_min" and num < 0:
            await update.message.reply_text("Мин. бюджет не может быть отрицательным.")
            return
        if key == "filter_budget_max" and num < 0:
            await update.message.reply_text("Макс. бюджет: 0 или больше.")
            return
        value = str(int(num)) if num == int(num) else str(num)

    await set_setting(key, value)
    context.user_data.pop("awaiting_setting", None)
    await update.message.reply_text(
        f"Сохранено: {FILTER_LABELS[key]}",
    )
    note = ""
    try:
        if await get_parse_mode() == PARSE_MODE_REALTIME and not (await _get_parser()).is_listening:
            note = "\n" + await _ensure_realtime(start=True)
    except Exception as exc:
        note = f"\nRealtime: {exc}"
    await update.message.reply_text(
        (await _settings_status_text()) + note,
        reply_markup=await _settings_keyboard(),
    )


async def live_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    action = query.data.split(":")[-1]
    try:
        if action == "start":
            msg = await _ensure_realtime(start=True)
        else:
            msg = await _ensure_realtime(start=False)
        await query.message.reply_text(
            f"{msg}\n\n" + await _realtime_status_text(),
            reply_markup=await _realtime_keyboard(),
        )
    except Exception as exc:
        logger.exception("Live control failed")
        await query.message.reply_text(f"Ошибка: {exc}")


async def channel_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    if not await are_filter_settings_complete():
        await query.message.reply_text(
            "Сначала заполни Настройки фильтра ИИ.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Открыть настройки", callback_data="settings:menu")]]
            ),
        )
        return
    try:
        channel_id = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.message.reply_text("Некорректный канал")
        return
    channel = await get_channel_by_id(channel_id)
    if not channel:
        await query.message.reply_text("Канал не найден")
        return
    username = channel.get("username") or channel.get("title") or str(channel_id)
    limits = [20, 50, 100, 200]
    keyboard = [
        [InlineKeyboardButton(str(n), callback_data=f"parser:run:{channel_id}:{n}")]
        for n in limits
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="mode:parser")])
    await query.message.reply_text(
        f"Канал: {username}\nСколько сообщений парсить?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _run_batch_job(message, username: str, limit: int) -> None:
    global _batch_running
    try:
        parser = await _get_parser()
        result = await parser.parse_channel(username, limit=limit)
        text = (
            f"Парсинг завершен\n"
            f"Канал: {result['channel']}\n"
            f"Всего: {result['parsed']}\n"
            f"Принято ИИ: {result['accepted']}\n"
            f"Пропущено (уже оценены): {result.get('skipped', 0)}\n"
        )
        await message.reply_text(text)
        for post in result.get("accepted_posts", []):
            msg, keyboard = _format_accepted_message(post)
            if not (post.get("text") or "").strip():
                continue
            await message.reply_text(
                msg,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except Exception as exc:
        logger.exception("Parser failed")
        await message.reply_text(f"Ошибка: {exc}")
    finally:
        _batch_running = False


async def run_parser_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _batch_running
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    if not await are_filter_settings_complete():
        await query.message.reply_text(
            "Сначала заполни Настройки фильтра ИИ.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Открыть настройки", callback_data="settings:menu")]]
            ),
        )
        return
    if _batch_running:
        await query.message.reply_text("Разовый парсинг уже идёт. Дождись завершения.")
        return
    parts = query.data.split(":")
    try:
        channel_id = int(parts[2])
        limit = int(parts[3])
    except (IndexError, ValueError):
        await query.message.reply_text("Некорректный запрос парсинга")
        return
    channel = await get_channel_by_id(channel_id)
    if not channel:
        await query.message.reply_text("Канал не найден")
        return
    username = channel.get("username") or ""
    if not username:
        await query.message.reply_text("У канала нет username")
        return
    _batch_running = True
    await query.message.reply_text(f"Парсим {username}, последние {limit} сообщений ...")
    asyncio.create_task(_run_batch_job(query.message, username, limit))


async def _show_response_menu(message) -> None:
    vacancies = await get_ai_vacancies(limit=20)
    if not vacancies:
        await message.reply_text("Нет принятых вакансий. Сначала запарсьте канал.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"{v['id']}. {v['channel']}",
                callback_data=f"response:generate:{v['id']}",
            )
        ]
        for v in vacancies
    ]
    keyboard.append([InlineKeyboardButton("Назад", callback_data="back:menu")])
    await message.reply_text(
        "Выбери вакансию для отклика",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def list_accepted_vacancies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    vacancy_id = int(query.data.split(":")[-1])
    await query.message.reply_text("Генерирую отклик...")
    try:
        vacancy = await get_vacancy_by_id(vacancy_id)
        if not vacancy or vacancy.get("ai_decision") != "ACCEPT":
            await query.message.reply_text("Вакансия не найдена")
            return
        reply_text = await generate_response_text(vacancy["text"])
        await add_response(vacancy_id, query.from_user.id, reply_text)
        # Plain message = easy long-press copy for any length.
        await query.message.reply_text(reply_text)
        hint = "Отослано в канал?"
        if len(reply_text) > COPY_TEXT_MAX:
            hint = "Зажми текст выше → Копировать.\n\nОтослано в канал?"
        await query.message.reply_text(
            hint,
            reply_markup=_response_actions_keyboard(vacancy_id, reply_text),
        )
    except Exception as exc:
        logger.exception("Response generation failed")
        await query.message.reply_text(f"Ошибка: {exc}")


async def confirm_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    parts = query.data.split(":")
    vacancy_id = int(parts[2])
    sent_flag = parts[3] == "1"
    updated = await set_response_sent(vacancy_id, sent_flag)
    if not updated:
        await query.message.reply_text("Отклик не найден.")
        return
    await query.message.reply_text(
        "Отмечено как отправленный." if sent_flag else "Ок, не отправляем."
    )


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not _only_admin(query.from_user.id):
        return
    await query.answer()
    _clear_awaiting(context)
    await query.message.reply_text(
        "Freelance parser\nВыбери действие в меню внизу.",
        reply_markup=_main_keyboard(),
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not _only_admin(update.effective_user.id):
        return
    await update.message.reply_text("Неизвестная команда. /start")


async def _post_init(application: Application) -> None:
    global _app
    _app = application

    # Register LLM API error notification callback
    async def _notify_llm_error(message: str) -> None:
        if _app is None:
            return
        try:
            await _app.bot.send_message(
                chat_id=settings.admin_telegram_id,
                text=message,
            )
        except Exception:
            logger.exception("Failed to send LLM error notification")

    set_notify_callback(_notify_llm_error)

    try:
        mode = await get_parse_mode()
        if mode == PARSE_MODE_REALTIME:
            msg = await _ensure_realtime(start=True)
            logger.info(msg)
    except Exception:
        logger.exception("Failed to autostart realtime parser")


async def _post_shutdown(application: Application) -> None:
    global _parser, _app
    try:
        if _parser is not None:
            await _parser.stop()
    except Exception:
        logger.exception("Shutdown parser failed")
    _parser = None
    _app = None


def build_bot() -> None:
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("add_channel", add_channel_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CallbackQueryHandler(mode_callback, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(settings_menu, pattern=r"^settings:menu$"))
    app.add_handler(CallbackQueryHandler(settings_toggle_mode, pattern=r"^settings:toggle_mode$"))
    app.add_handler(CallbackQueryHandler(settings_edit, pattern=r"^settings:edit:"))
    app.add_handler(CallbackQueryHandler(live_control_callback, pattern=r"^parser:live:(start|stop)$"))
    app.add_handler(CallbackQueryHandler(channel_limit_callback, pattern=r"^parser:channel:\d+$"))
    app.add_handler(CallbackQueryHandler(run_parser_callback, pattern=r"^parser:run:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(list_accepted_vacancies, pattern=r"^response:generate:\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_sent, pattern=r"^response:confirm:\d+:[01]$"))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern=r"^back:menu$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, settings_text_input))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    app.run_polling()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_bot()


if __name__ == "__main__":
    main()
