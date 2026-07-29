# DocSift Agent

Локальное приложение для извлечения, проверки и экспорта данных из российских бухгалтерских документов.

## Возможности

- загрузка PDF, PNG, JPG и TIFF;
- извлечение текста и табличных позиций;
- классификация бухгалтерских документов;
- проверка ИНН, КПП, дат, сумм и НДС;
- ручное исправление полей и позиций;
- подтверждение предупреждений перед завершением проверки;
- экспорт проверенных данных в XLSX;
- удаление ошибочно загруженных документов;
- светлая и тёмная темы;
- отчёты по качеству и сравнение запусков.

## Поддерживаемые типы

- счёт на оплату (`payment_invoice`);
- счёт-фактура (`vat_invoice`);
- универсальный передаточный документ (`universal_transfer_document`);
- товарная накладная ТОРГ-12 (`consignment_note_torg12`);
- акт выполненных работ или услуг (`work_completion_act`).

## Стек

- Python 3.13;
- FastAPI и Jinja2;
- SQLAlchemy и Alembic;
- PostgreSQL;
- Pydantic;
- HTMX и Alpine.js;
- Ollama или OpenAI-compatible LLM;
- PyMuPDF, OCR и OpenPyXL;
- Pytest.

## Локальный запуск

### 1. Настройки

```powershell
Copy-Item .env.example .env
```

Заполните локальные параметры в `.env`. Файл `.env` не должен попадать в Git.

### 2. PostgreSQL

```powershell
docker compose up -d postgres
```

### 3. Миграции

```powershell
.\.venv\Scripts\python.exe -X utf8 -m alembic upgrade head
```

### 4. Приложение

```powershell
.\.venv\Scripts\python.exe -X utf8 -m uvicorn docsift.main:app --host 127.0.0.1 --port 8000
```

Интерфейс будет доступен по адресу <http://127.0.0.1:8000>.

## Тесты

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

## Структура

```text
src/docsift/   приложение и бизнес-логика
tests/         автоматические тесты
migrations/    миграции базы данных
datasets/      тестовый бухгалтерский датасет
docs/          техническая документация
tools/         вспомогательные инструменты
```

## Безопасность

- не публикуйте `.env`, API-ключи и `DOCSIFT_WEB_SECRET`;
- используйте отдельный случайный web secret для каждого окружения;
- перед публикацией проверяйте список staged-файлов.
