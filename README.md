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

## Поведение при ошибках LLM

- **401 / прочие 4xx** — без ретраев; после 3 подряд неудач админу летит уведомление (не чаще 1 раза в 5 минут).
- **402 (баланс/токены исчерпаны)** — первое уведомление приходит **немедленно**, фильтрация ставится на паузу на **10 минут**: вызовы LLM не тратятся, вакансии остаются в базе и переоцениваются после пополнения баланса. Повторные 402 в течение паузы не спамят.
- **429 / 5xx / таймауты** — до 3 ретраев с экспоненциальным бэкоффом (1с → 2с → 4с).

Проверить текущее состояние можно командой `/status`.

## Переменные окружения

Обязательные: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `ADMIN_TELEGRAM_ID`, `LLM_API_KEY`.

Опциональные:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | База OpenAI-совместимого API |
| `LLM_MODEL` | `gpt-4o-mini` | Модель для фильтрации и откликов |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DATABASE_PATH` | `./freelance_parser.db` | Путь к файлу базы |
| `PARSER_SESSION_NAME` | `freelance_parser` | Имя Telethon-сессии |

## Тесты

```bash
pip install -e ".[dev]"   # ставит pytest и pytest-asyncio
python -m pytest tests/ -v
```

52 теста покрывают: валидацию входа каналов (`parse_channel_input`), retry-логику с бэкоффом (замоканный HTTP — без сети), кулдаун при 402 и парсинг ответов LLM.
