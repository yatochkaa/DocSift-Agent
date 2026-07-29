# Этап 0: схемы документов и хранения

## Метаданные извлечения

Рассмотрены два подхода.

1. **Generic-обёртка `ExtractedField[T]`**: рядом со значением находятся `confidence` и
   `sources`. Плюсы: значение нельзя отделить от доказательства, тип проверяется Pydantic,
   ручная проверка получает готовый источник. Минус: JSON становится объёмнее.
2. **Отдельная карта метаданных**: обычный документ плюс
   `evidence: dict[JSONPointer, FieldEvidence]`. Плюс: бизнес-JSON компактнее. Минусы: пути
   ломаются при перестановке позиций, сложнее гарантировать покрытие всех полей, исправление
   массива может отвязать доказательство от значения.

Выбран первый подход. Повторение скрыто одной generic-моделью, а строгая связь значения с
источником важнее размера JSON. Для отсутствующего значения разрешены `value=null`,
`confidence=0`, `sources=[]`.

`SourceRef` поддерживает PDF-текст, OCR, фото и Excel. Координаты bbox нормализованы в диапазон
0..1, поэтому не зависят от DPI. Для Excel используются лист и диапазон ячеек.

## Валидация

- ИНН: 10 или 12 цифр и официальные контрольные суммы.
- КПП: 9 цифр; контрольной суммы у КПП нет.
- Денежные суммы и НДС: неотрицательные `Decimal`.
- Количество: строго больше нуля.
- Даты выпуска, отгрузки, договора и периода услуг: не в будущем.
- Срок оплаты может быть в будущем, поэтому для него запрет будущей даты намеренно не задан.

## ERD

```text
documents (1) ───────< (N) extractions (1) ───────< (N) review_tasks

documents
  PK id UUID
  original_filename, object_key UNIQUE, content_type, size_bytes, sha256
  status, detected_type, created_at, updated_at

extractions
  PK id UUID
  FK document_id -> documents.id ON DELETE CASCADE
  UNIQUE(document_id, attempt_no)
  status, schema_version, provider, model, prompt_version
  provider_settings JSONB, result JSONB
  overall_confidence, requires_review, timestamps, error fields

review_tasks
  PK id UUID
  FK extraction_id -> extractions.id ON DELETE CASCADE
  field_path (JSON Pointer), reason, status
  original_value JSONB, corrected_value JSONB
  reviewer_id, resolved_at, resolution_comment, timestamps

eval_runs
  PK id UUID
  dataset_name/version, schema_version, provider, model, prompt_version
  run_config JSONB, metrics JSONB, sample_count, git_sha
  status, started_at, completed_at, error_message, timestamps
```

`eval_runs` не связан с пользовательскими документами: eval-набор должен быть обезличен и
версионирован отдельно. Если позже понадобятся метрики по каждому примеру, добавляется дочерняя
таблица `eval_cases`, а не JSON-массив в `eval_runs`.

Исходный файл не хранится в PostgreSQL: только ключ приватного object storage и SHA-256. JSONB
содержит коммерческие данные, поэтому в продакшене обязательны шифрование диска/бэкапов,
изоляция tenant-данных и запрет секретов в `provider_settings`.

## Структура

```text
src/docsift/
├── api/               # роутеры и Depends
├── core/              # конфигурация, логирование, инфраструктура
├── domain/            # общие enum и доменные правила
├── db/models/         # только ORM-модели
├── schemas/           # Pydantic-схемы API и извлечения
├── repositories/      # единственная точка доступа к БД
└── services/          # бизнес-логика и orchestration
tests/                 # тесты схем и моделей
docs/                  # архитектурные решения
```

