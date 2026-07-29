"""Презентеры: превращают данные домена в готовые к рендеру dataclass-и.

Правило: в шаблонах нет никакой логики — только обход готовых структур.
Все функции здесь чистые: принимают простые структуры (dict/модели) и ничего не знают
ни о HTTP, ни о сессии БД — их можно тестировать без сервера.
"""

from .common import Chip, Delta, KpiCard, Sparkline, WaterfallRow, build_waterfall, make_delta
from .compare import ComparePage, build_compare
from .dashboard import DashboardPage, build_dashboard
from .document_detail import DocumentDetailPage, build_document_detail
from .documents import DocumentsPage, build_documents
from .evals import EvalReportPage, EvalsPage, build_eval_report, build_evals
from .upload import UploadCard, UploadStage, build_upload_card, card_from_document

__all__ = [
    "Chip",
    "UploadCard",
    "UploadStage",
    "build_upload_card",
    "card_from_document",
    "Delta",
    "KpiCard",
    "Sparkline",
    "WaterfallRow",
    "build_waterfall",
    "make_delta",
    "ComparePage",
    "build_compare",
    "DashboardPage",
    "build_dashboard",
    "DocumentDetailPage",
    "build_document_detail",
    "DocumentsPage",
    "build_documents",
    "EvalsPage",
    "EvalReportPage",
    "build_evals",
    "build_eval_report",
]
