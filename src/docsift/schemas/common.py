from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceKind(StrEnum):
    PDF_TEXT = "pdf_text"
    OCR = "ocr"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"


class BoundingBox(SchemaModel):
    """Нормализованные координаты: левый верхний и правый нижний углы."""

    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("bbox должен иметь положительные ширину и высоту")
        return self


class SourceRef(SchemaModel):
    """Ссылка на фрагмент исходника без хранения самого документа в JSON."""

    kind: SourceKind
    page: int | None = Field(default=None, ge=1)
    bbox: BoundingBox | None = None
    sheet: str | None = None
    cell_range: str | None = None
    text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        if self.kind is SourceKind.SPREADSHEET:
            if not self.sheet or not self.cell_range:
                raise ValueError("для Excel нужны sheet и cell_range")
        elif self.page is None:
            raise ValueError("для PDF, OCR и изображения нужен номер страницы")
        return self


_CONFIDENCE_EPSILON = 1e-12


def _snap_confidence(value: Any) -> Any:
    """Снять погрешность float у ``confidence`` на границах диапазона.

    Локальные модели возвращают значения вроде ``1.0000000000000002``:
    формально вне ``[0, 1]``, фактически — единица. Отклонения больше
    ``_CONFIDENCE_EPSILON`` (``1.01``, ``-0.01``) остаются ошибкой и
    отклоняются ``Field(ge=0, le=1)``. Нечисловое значение и ``bool``
    возвращаются как есть — их разбирает сам Pydantic.

    Неизменённое значение возвращается тем же объектом, что позволяет
    вызывающему коду отличить правку по ``is``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if 1 < value <= 1 + _CONFIDENCE_EPSILON:
        return 1.0
    if -_CONFIDENCE_EPSILON <= value < 0:
        return 0.0
    return value


class ExtractedField[T](SchemaModel):
    """Значение вместе с уверенностью и подтверждающими фрагментами."""

    value: T | None
    confidence: float = Field(ge=0, le=1)
    sources: list[SourceRef] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_missing(cls, data: Any) -> Any:
        """Привести отсутствующее значение к ``confidence=0`` без источников.

        LLM регулярно возвращает ``{"value": null, "confidence": 1.0}``.
        Канонизация выполняется до проверки инварианта в
        :meth:`validate_evidence` и не трогает корректные непустые поля.
        Здесь же снимается погрешность float у ``confidence``: у
        отсутствующего значения она не важна, поэтому применяется только
        к непустым полям. Вход не мутируется: при необходимости создаётся копия.
        """
        if isinstance(data, dict):
            confidence = data.get("confidence")
            if "value" in data and data["value"] is None:
                if confidence or data.get("sources"):
                    data = {**data, "confidence": 0, "sources": []}
            else:
                snapped = _snap_confidence(confidence)
                if snapped is not confidence:
                    data = {**data, "confidence": snapped}
        elif isinstance(data, BaseModel) and getattr(data, "value", "") is None:
            if data.confidence or data.sources:
                data = data.model_copy(update={"confidence": 0, "sources": []})
        return data

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.value is not None and not self.sources:
            raise ValueError("для извлечённого значения нужен хотя бы один source")
        if self.value is None and self.confidence != 0:
            raise ValueError("у отсутствующего значения confidence должен быть равен 0")
        return self


def validate_inn(value: str) -> str:
    if not value.isdigit() or len(value) not in (10, 12):
        raise ValueError("ИНН должен содержать 10 или 12 цифр")

    digits = [int(char) for char in value]
    if len(digits) == 10:
        checksum = sum(a * b for a, b in zip(digits[:9], (2, 4, 10, 3, 5, 9, 4, 6, 8)))
        if checksum % 11 % 10 != digits[9]:
            raise ValueError("неверная контрольная сумма ИНН")
    else:
        checksum_11 = sum(
            a * b for a, b in zip(digits[:10], (7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        )
        checksum_12 = sum(
            a * b for a, b in zip(digits[:11], (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8))
        )
        if checksum_11 % 11 % 10 != digits[10] or checksum_12 % 11 % 10 != digits[11]:
            raise ValueError("неверная контрольная сумма ИНН")
    return value


def validate_not_future(value: date) -> date:
    if value > datetime.now(UTC).date():
        raise ValueError("дата документа не может быть в будущем")
    return value


# ---------------------------------------------------------------------------
# Нормализация валюты: нестандартные обозначения → ISO 4217
# ---------------------------------------------------------------------------

_CURRENCY_ALIASES: dict[str, str] = {
    "руб.": "RUB",
    "руб": "RUB",
    "₽": "RUB",
    "российский рубль": "RUB",
    "российские рубли": "RUB",
    "rur": "RUB",
    "rub": "RUB",
    "usd": "USD",
    "$": "USD",
    "евро": "EUR",
    "eur": "EUR",
}


def normalize_currency(value: str) -> str:
    """Нормализовать название валюты в ISO 4217 трёхбуквенный код.

    Убирает пробелы по краям, приводит к нижнему регистру для поиска в
    словаре. Если значение не найдено — возвращает как есть (валидация
    ``StringConstraints`` отклонит некорректный код).
    """
    key = value.strip().casefold()
    return _CURRENCY_ALIASES.get(key, value)


InnCandidate = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^\d{10}(\d{2})?$"),
]
Inn = Annotated[InnCandidate, AfterValidator(validate_inn)]
Kpp = Annotated[str, StringConstraints(strict=True, pattern=r"^\d{9}$")]
CurrencyCode = Annotated[
    str,
    BeforeValidator(normalize_currency),
    StringConstraints(strict=True, to_upper=True, pattern=r"^[A-Z]{3}$"),
]
NonFutureDate = Annotated[date, AfterValidator(validate_not_future)]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]
UnitPrice = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
VatPercent = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
