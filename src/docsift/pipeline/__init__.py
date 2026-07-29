"""Пайплайн приёма документа: сохранение → текст → LLM → guardrails.

Публичная точка входа — :func:`ingest_document`: сохраняет файл, заводит
``Document`` и запускает асинхронную обработку, не блокируя ответ загрузки.
"""

from docsift.pipeline.ingest import ingest_document

__all__ = ["ingest_document"]