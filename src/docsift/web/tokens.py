"""Единственное место, где заданы «магические» значения интерфейса.

В шаблонах нельзя хардкодить цвета, пороги и подписи статусов — всё берётся
отсюда через презентеры или через глобали Jinja (см. app.build_templates).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ToneName = Literal["success", "warning", "danger", "accent", "muted", "neutral"]

# --- Палитра (дублирует theme.css; здесь нужна для canvas-графиков) ---------
PALETTE: dict[str, str] = {
    "bg": "#0B0D10",
    "surface": "#14171C",
    "raised": "#1B1F26",
    "border": "#252A33",
    "text": "#E7EAEE",
    "muted": "#98A2B3",
    "accent": "#6366F1",
    "success": "#10B981",
    "warning": "#F59E0B",
    "danger": "#EF4444",
}

# --- Пороги уверенности -----------------------------------------------------
CONFIDENCE_HIGH = 0.9
CONFIDENCE_MEDIUM = 0.7


def confidence_tone(value: float | None) -> ToneName:
    """Цветовой тон чипа уверенности: >=0.9 зелёный, 0.7–0.9 жёлтый, <0.7 красный."""
    if value is None:
        return "muted"
    if value >= CONFIDENCE_HIGH:
        return "success"
    if value >= CONFIDENCE_MEDIUM:
        return "warning"
    return "danger"


# --- Статусы -----------------------------------------------------------------
# Здесь сведены значения двух разных перечислений: ExtractionStatus
# (pending/succeeded/...) и DocumentStatus (uploaded/extracted/review_required/
# ...). Раньше карта покрывала только первое, поэтому статусы документа
# доезжали до пользователя сырыми ключами — «review_required» вместо подписи.
# Подписи здесь короткие: их место — чип в таблице. Развёрнутые формулировки
# для экрана загрузки лежат ниже, в UPLOAD_*.
STATUS_LABELS: dict[str, str] = {
    "pending": "В очереди",
    "processing": "Обработка",
    "completed": "Готово",
    "failed": "Ошибка",
    "partial": "Частично",
    "uploaded": "Загружен",
    "extracted": "Извлечён",
    "review_required": "Требуется проверка",
    "succeeded": "Готово",
    "running": "Обработка",
}

STATUS_TONES: dict[str, ToneName] = {
    "pending": "muted",
    "processing": "accent",
    "completed": "success",
    "failed": "danger",
    "partial": "warning",
    "uploaded": "muted",
    "extracted": "accent",
    "review_required": "warning",
    "succeeded": "success",
    "running": "accent",
}


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "—")


def status_tone(status: str | None) -> ToneName:
    return STATUS_TONES.get(status or "", "neutral")


# --- Ход обработки загруженного файла ---------------------------------------
# Экран загрузки говорит с пользователем полными фразами, а не ярлыками чипа:
# на этом шаге человек ждёт результат и должен понимать, что происходит.
UPLOAD_STATUS_LABELS: dict[str, str] = {
    "uploaded": "Файл загружен",
    "processing": "Извлекаем данные",
    "extracted": "Проверяем данные",
    "review_required": "Требуется проверка",
    "completed": "Обработка завершена",
    "succeeded": "Обработка завершена",
    "failed": "Ошибка обработки",
}

# Заголовок карточки — то, что пользователь читает первым.
UPLOAD_HEADLINES: dict[str, str] = {
    "uploaded": "Файл загружен. Начинаем обработку",
    "processing": "Извлекаем данные из документа",
    "extracted": "Проверяем извлечённые данные",
    "review_required": "Документ готов к проверке",
    "completed": "Документ успешно обработан",
    "succeeded": "Документ успешно обработан",
    "failed": "Не удалось обработать документ",
}

# Три видимых этапа. Обработка коротка, дробить её мельче — только пугать
# пользователя мельканием; «extracted» это всё ещё второй этап.
UPLOAD_STAGE_NAMES: tuple[str, str, str] = (
    "Файл загружен",
    "Извлекаем данные",
    "Результат готов",
)

UPLOAD_STAGE_INDEX: dict[str, int] = {
    "uploaded": 1,
    "processing": 2,
    "extracted": 2,
    "review_required": 3,
    "completed": 3,
    "succeeded": 3,
    "failed": 3,
}

# Статусы, на которых обработка ещё идёт и карточку нужно опрашивать.
UPLOAD_PENDING_STATUSES: frozenset[str] = frozenset(
    {"uploaded", "processing", "extracted", "pending", "running"}
)

UPLOAD_FAILED_STATUSES: frozenset[str] = frozenset({"failed"})


def upload_status_label(status: str | None) -> str:
    """Подпись этапа для экрана загрузки; сырой ключ наружу не попадает."""
    key = (status or "").strip()
    return UPLOAD_STATUS_LABELS.get(key) or status_label(key)


def upload_headline(status: str | None) -> str:
    key = (status or "").strip()
    return UPLOAD_HEADLINES.get(key) or upload_status_label(key)


def upload_stage_index(status: str | None) -> int:
    return UPLOAD_STAGE_INDEX.get((status or "").strip(), 1)


def is_upload_pending(status: str | None) -> bool:
    return (status or "").strip() in UPLOAD_PENDING_STATUSES


# --- Ошибки обработки --------------------------------------------------------
# Пользователю показываем только эти формулировки. Extraction.error_message —
# это str(exc): там бывает сырой ответ провайдера и куски JSON, поэтому в
# интерфейс он не идёт никогда, только код ошибки.
UPLOAD_ERROR_TEXTS: dict[str, str] = {
    "provider_error": "Модель извлечения не ответила. Попробуйте загрузить файл позже.",
    "schema_validation_failed": (
        "Модель вернула данные, которые не проходят проверку схемы. "
        "Документ сохранён — посмотрите подробности."
    ),
    "text_extraction_failed": (
        "Не удалось прочитать текст из файла. "
        "Возможно, это скан без текстового слоя или повреждённый PDF."
    ),
    "upload_too_large": "Файл больше допустимого размера.",
    "unsupported_content_type": "Такой тип файла не поддерживается.",
}

UPLOAD_ERROR_FALLBACK = "Обработка прервалась из-за внутренней ошибки. Документ сохранён."


def upload_error_text(error_code: str | None) -> str:
    return UPLOAD_ERROR_TEXTS.get((error_code or "").strip(), UPLOAD_ERROR_FALLBACK)


# --- Шаги обработки ---------------------------------------------------------
STEP_LABELS: dict[str, str] = {
    "text_extraction": "Извлечение текста",
    "llm_extraction": "LLM-экстракция",
    "metrics": "Метрики",
    "guardrails": "Guardrails",
    "other": "Прочее",
}

STEP_COLORS: dict[str, str] = {
    "text_extraction": PALETTE["accent"],
    "llm_extraction": "#22D3EE",
    "metrics": PALETTE["success"],
    "guardrails": PALETTE["warning"],
    "other": PALETTE["muted"],
}


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


def step_color(step: str) -> str:
    return STEP_COLORS.get(step, PALETTE["muted"])


# --- Тепловая карта метрик --------------------------------------------------
@dataclass(frozen=True)
class HeatCell:
    """Ячейка тепловой карты: значение + inline-стиль заливки."""

    value: float | None
    text: str
    style: str
    tone: ToneName


def heat_cell(value: float | None) -> HeatCell:
    """Заливка ячейки метрики: от красного к зелёному, прозрачность по значению."""
    if value is None:
        return HeatCell(None, "—", "", "muted")
    v = max(0.0, min(1.0, float(value)))
    tone: ToneName = confidence_tone(v)
    base = {"success": "16,185,129", "warning": "245,158,11", "danger": "239,68,68", "muted": "152,162,179"}[tone]
    # Насыщенность 10–28%: заливка не должна забивать текст (контраст AA).
    alpha = round(0.10 + 0.18 * v, 3)
    return HeatCell(v, f"{v * 100:.1f}%".replace(".", ","), f"background-color: rgba({base}, {alpha});", tone)


# --- Дельты -----------------------------------------------------------------
def delta_tone(delta: float | None, higher_is_better: bool = True) -> ToneName:
    """Зелёный, если изменение в нужную сторону; красный — если нет."""
    if delta is None or abs(delta) < 1e-9:
        return "neutral"
    improved = delta > 0 if higher_is_better else delta < 0
    return "success" if improved else "danger"


def delta_arrow(delta: float | None) -> str:
    if delta is None or abs(delta) < 1e-9:
        return "→"
    return "↑" if delta > 0 else "↓"
