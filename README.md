# Freelance Telegram Channel Parser

Telegram-бот для двухрежимного парсинга фриланс-вакансий:
1. Парсер — чтение каналов, фильтрация через ИИ
2. Ответы — генерация текстов откликов под вакансию

## Быстрый старт

```bash
cp .env.example .env
# заполни .env
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m src.main
```

## Деплой на VPS

### Через Docker

```bash
# 1. Склонируй репозиторий
git clone <repo-url> && cd freelance-channel-parser

# 2. Создай .env файл
cp .env.example .env
nano .env  # заполни все переменные

# 3. Запусти
sudo docker compose up -d

# 4. Проверь логи
sudo docker compose logs -f
```

### Управление

```bash
# Остановить
sudo docker compose down

# Перезапустить
sudo docker compose restart

# Обновить и пересобрать
sudo docker compose up -d --build

# Посмотреть логи
sudo docker compose logs -f bot
```

### Автообновление при пуше

Если используешь CI/CD (GitHub Actions, GitLab CI), добавь хук на pull:

```bash
# /opt/freelance-parser/deploy.sh
cd /opt/freelance-parser
git pull
sudo docker compose up -d --build
```

## Команды бота

| Команда | Описание |
|---|---|
| `/start`, `/help` | Главное меню |
| `/add_channel` | Добавить канал (ссылкой или `@username`) |
| `/channels` | Список каналов в базе |
| `/status` | Диагностика: модель LLM, живой пинг LLM API (различает 200 / 401 / 402), состояние квоты, realtime и заполненность фильтра |
| `/set_key` | Задать LLM API ключ прямо в чате (вариант: `/set_key sk-...`) |
| `/forget_key` | Удалить ключ из чата и вернуться к ключу из `.env` |
| `/set_model` | Сменить модель прямо в чате (вариант: `/set_model llama-3.3-70b-versatile`) |
| `/forget_model` | Вернуться к модели из `.env` |

## Поведение при ошибках LLM

- **401 / прочие 4xx** — без ретраев; после 3 подряд неудач админу летит уведомление (не чаще 1 раза в 5 минут).
- **402 (баланс/токены исчерпаны)** — первое уведомление приходит **немедленно**, фильтрация ставится на паузу на **10 минут**: вызовы LLM не тратятся, вакансии остаются в базе и переоцениваются после пополнения баланса. Повторные 402 в течение паузы не спамят.
- **429 / 5xx / таймауты** — до 3 ретраев с экспоненциальным бэкоффом (1с → 2с → 4с).

Проверить текущее состояние можно командой `/status`.

## Провайдер LLM: Groq

По умолчанию используется **Groq** — OpenAI-совместимый API с бесплатным тарифом:

- `LLM_BASE_URL` = `https://api.groq.com/openai/v1`
- `LLM_MODEL` = `llama-3.3-70b-versatile`

Ключ берётся в [console.groq.com](https://console.groq.com/keys) (`GROQ_API_KEY` → `LLM_API_KEY` в `.env`). Актуальный список моделей: `GET /models` или [console.groq.com/docs/models](https://console.groq.com/docs/models).

Популярные модели:

| ID | Особенность |
|---|---|
| `llama-3.3-70b-versatile` | Лучшее качество (дефолт) |
| `llama-3.1-8b-instant` | Самая быстрая |
| `openai/gpt-oss-120b` | Флагман OpenAI (open-weight) |
| `openai/gpt-oss-20b` | Дешёвая и быстрая |

Любой OpenAI-совместимый провайдер можно подключить, задав свой `LLM_BASE_URL`.

## Смена API-ключа и модели через чат

Ключ и модель можно менять без правки `.env` и перезапуска:

**Ключ:**
1. `/set_key` — бот спросит ключ (или сразу: `/set_key sk-...`).
2. Пришли ключ одним сообщением — бот **удалит твоё сообщение с ключом** и проверит ключ живым запросом к API (🟢 200 / 🔴 401 / 🟡 402).
3. Ключ сохраняется в базе и **переживает перезапуск** бота.

Откатить: `/forget_key` — вернуться к ключу из `.env`.

⚠️ Сообщение с ключом на секунду видно в чате до удаления — для максимальной безопасности задавай ключ в `.env`.

**Модель:**
1. `/set_model` — бот покажет популярные модели Groq (или сразу: `/set_model llama-3.1-8b-instant`).
2. ID проверяется по списку моделей провайдера — несуществующая модель не сохранится.

Откатить: `/forget_model` — вернуться к модели из `.env`.

## Переменные окружения

Обязательные: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_TELEGRAM_ID`, `LLM_API_KEY`.

Опциональные:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | База OpenAI-совместимого API |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Модель для фильтрации и откликов |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DATABASE_PATH` | `./freelance_parser.db` | Путь к файлу базы |
| `PARSER_SESSION_NAME` | `freelance_parser` | Имя Telethon-сессии |

## Тесты

```bash
pip install -e ".[dev]"   # ставит pytest и pytest-asyncio
python -m pytest tests/ -v
```

69 тестов покрывают: валидацию входа каналов (`parse_channel_input`), retry-логику с бэкоффом (замоканный HTTP — без сети), кулдаун при 402, пинг API, парсинг ответов LLM и смену ключа/модели через чат.
