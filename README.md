# Ozon/WB Auto Updater

Десктопное приложение (Python + Tkinter) для автоматического обновления названий и описаний товаров на Ozon и Wildberries с помощью AI (Claude или Gemini).

## Возможности

- Генерация SEO-описаний для карточек Ozon и WB через Claude или Gemini
- Сбор реальных ключевых слов из аналитики Ozon Seller API и MPStats, с запасным вариантом — поиском через веб-поиск Claude, если аналитики ещё нет (новый товар)
- Анализ карточек конкурентов: сначала через публичный поиск маркетплейса, при недоступности — через веб-поиск Claude
- Предпросмотр «было/стало» перед применением изменений, ручной выбор товаров для обработки
- Сравнение и синхронизация цен между Ozon, WB и MPStats
- Копирование названия и описания с Ozon на WB для товаров, сопоставленных по артикулу
- Постраничная работа с большими каталогами, ретраи и обработка лимитов API

## Требования

- Python 3.10+
- Tkinter (обычно идёт в комплекте с Python; на Linux может потребоваться `sudo apt install python3-tk`)

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

1. Скопируйте `config.example.json` в `config.json`:
   ```bash
   cp config.example.json config.json
   ```
2. Заполните `config.json` своими ключами:
   - `ozon.client_id` / `ozon.api_key` — Ozon Seller API ([личный кабинет продавца](https://seller.ozon.ru) → Настройки → API-ключи)
   - `wb.api_key` — токен Wildberries Content API
   - `mpstats.token` — токен [mpstats.io](https://mpstats.io) (опционально)
   - `ai.anthropic_api_key` — ключ [Anthropic API](https://console.anthropic.com) (для генерации описаний и веб-поиска конкурентов/ключевых слов)
   - `ai.gemini_api_key` — ключ Google Gemini (опционально, альтернативный провайдер генерации)

**`config.json` содержит секреты и не должен попадать в репозиторий** — он уже добавлен в `.gitignore`.

## Запуск

```bash
python ozon_auto_updater.py
```

## Структура проекта

- `ozon_auto_updater.py` — основное приложение (GUI, клиенты API, генерация описаний, синхронизация цен)
- `check_hashtags.py` — вспомогательный скрипт для проверки атрибута хештегов у нескольких товаров Ozon
- `config.example.json` — шаблон конфигурации без реальных ключей

## Лицензия

Не определена. Добавьте файл LICENSE, если планируете публичное распространение под конкретной лицензией.
