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
