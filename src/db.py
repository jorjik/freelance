import aiosqlite
import shutil
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_DB_PATH = Path("/root/.freelance_parser.db")


def _get_db_path() -> Path:
    from src.config import settings
    raw = (settings.database_path or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p
    return PROJECT_ROOT / "freelance_parser.db"


def _db_path() -> Path:
    return _get_db_path()

FILTER_KEYS = (
    "filter_stack",
    "filter_budget_min",
    "filter_budget_max",
    "filter_take",
    "filter_skip",
    "filter_notes",
)

PARSE_MODE_KEY = "parse_mode"
PARSE_MODE_REALTIME = "realtime"
PARSE_MODE_BATCH = "batch"
DEFAULT_PARSE_MODE = PARSE_MODE_REALTIME


def _migrate_old_db() -> None:
    if _db_path().exists():
        return
    if OLD_DB_PATH.exists():
        shutil.copy2(OLD_DB_PATH, _db_path())


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


async def init_db() -> None:
    _migrate_old_db()
    async with aiosqlite.connect(_db_path()) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                title TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                media_paths TEXT,
                parse_mode TEXT,
                price_min REAL,
                price_max REAL,
                currency TEXT,
                ai_decision TEXT,
                ai_reason TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(channel_id) REFERENCES channels(id),
                UNIQUE(channel_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vacancy_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(vacancy_id) REFERENCES vacancies(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        await _ensure_column(db, "responses", "sent", "sent INTEGER NOT NULL DEFAULT 0")
        await db.commit()


async def get_setting(key: str) -> Optional[str]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_filter_settings() -> dict[str, str]:
    await init_db()
    result = {k: "" for k in FILTER_KEYS}
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in FILTER_KEYS)})",
            FILTER_KEYS,
        ) as cur:
            async for row in cur:
                result[row[0]] = row[1] or ""
    return result


LLM_API_KEY_SETTING = "llm_api_key"


async def get_llm_api_key() -> Optional[str]:
    """API key set via chat (overrides the .env one). None if not set."""
    value = await get_setting(LLM_API_KEY_SETTING)
    value = (value or "").strip()
    return value or None


async def get_parse_mode() -> str:
    value = (await get_setting(PARSE_MODE_KEY) or "").strip().lower()
    if value in (PARSE_MODE_REALTIME, PARSE_MODE_BATCH):
        return value
    return DEFAULT_PARSE_MODE


async def set_parse_mode(mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode not in (PARSE_MODE_REALTIME, PARSE_MODE_BATCH):
        mode = DEFAULT_PARSE_MODE
    await set_setting(PARSE_MODE_KEY, mode)
    return mode


async def get_vacancy_decision(vacancy_id: int) -> Optional[str]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            "SELECT ai_decision FROM vacancies WHERE id = ?", (vacancy_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_vacancy_by_id(vacancy_id: int) -> Optional[dict]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT v.id, v.text, v.ai_decision, v.ai_reason, v.status,
                   c.username AS channel, c.id AS channel_id
            FROM vacancies v
            JOIN channels c ON c.id = v.channel_id
            WHERE v.id = ?
            """,
            (vacancy_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_channel_by_id(channel_id: int) -> Optional[dict]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, title, is_active, added_at FROM channels WHERE id = ?",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def are_filter_settings_complete() -> bool:
    data = await get_filter_settings()
    return any(str(data.get(k, "")).strip() for k in FILTER_KEYS)


async def add_channel(username: str, title: Optional[str] = None) -> int:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO channels(username, title) VALUES(?, ?)",
            (username, title),
        )
        await db.commit()
        async with db.execute("SELECT id FROM channels WHERE username = ?", (username,)) as cur:
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError(f"Channel {username} not found after insert")
            return int(row["id"])


async def upsert_vacancy(
    channel_id: int,
    message_id: int,
    text: str,
    media_paths: Optional[str] = None,
    parse_mode: Optional[str] = None,
) -> tuple[Optional[int], bool]:
    """Return (vacancy_id, is_new). is_new=False if row already existed."""
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            INSERT INTO vacancies(channel_id, message_id, text, media_paths, parse_mode)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, message_id) DO NOTHING
            """,
            (channel_id, message_id, text, media_paths, parse_mode),
        )
        await db.commit()
        is_new = cur.rowcount > 0
        async with db.execute(
            "SELECT id FROM vacancies WHERE channel_id = ? AND message_id = ?",
            (channel_id, message_id),
        ) as sel:
            row = await sel.fetchone()
            if row is None:
                return None, False
            return int(row["id"]), is_new


async def update_vacancy_ai(vacancy_id: int, decision: str, reason: str) -> None:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE vacancies SET ai_decision = ?, ai_reason = ? WHERE id = ?",
            (decision, reason, vacancy_id),
        )
        await db.commit()


async def add_response(vacancy_id: int, telegram_id: int, text: str) -> int:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute(
            "INSERT INTO responses(vacancy_id, telegram_id, text, sent) VALUES(?, ?, ?, 0)",
            (vacancy_id, telegram_id, text),
        )
        await db.commit()
        return int(cur.lastrowid)


async def set_response_sent(vacancy_id: int, sent: bool) -> bool:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute(
            """
            UPDATE responses
            SET sent = ?
            WHERE id = (
                SELECT id FROM responses
                WHERE vacancy_id = ?
                ORDER BY id DESC
                LIMIT 1
            )
            """,
            (1 if sent else 0, vacancy_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_latest_response_text(vacancy_id: int) -> Optional[str]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        async with db.execute(
            """
            SELECT text FROM responses
            WHERE vacancy_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (vacancy_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_ai_vacancies(limit: int = 20) -> list[dict]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT v.id, v.text, c.username AS channel, v.ai_reason
            FROM vacancies v
            JOIN channels c ON c.id = v.channel_id
            WHERE v.ai_decision = 'ACCEPT'
            ORDER BY v.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


async def get_channels(limit: int = 50) -> list[dict]:
    await init_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, username, title, is_active, added_at FROM channels ORDER BY added_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]
