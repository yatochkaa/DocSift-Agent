from __future__ import annotations

from datetime import date
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, model_validator

from docsift.domain.enums import DocumentType
from docsift.schemas.common import (
    CurrencyCode,
    ExtractedField,
    InnCandidate,
    Kpp,
    Money,
    NonFutureDate,
    Quantity,
    SchemaModel,
    UnitPrice,
    VatPercent,
)

VatRate = VatPercent | Literal["without_vat"]


class Party(SchemaModel):
    name: ExtractedField[str]
    inn: ExtractedField[InnCandidate]
    kpp: ExtractedField[Kpp]

    @model_validator(mode="before")
    @classmethod
    def _normalize_kpp_ogrnip(cls, data: Any) -> Any:
        """Если в поле ``kpp`` попал ОГРН (13 цифр) или ОГРНИП (15), вернуть ``null``.

        Обнуление ``confidence`` и очистка ``sources`` выполняются общей
        канонизацией в :class:`ExtractedField`. Вход не мутируется.
        """
        if isinstance(data, dict):
            kpp = data.get("kpp")
            if isinstance(kpp, dict):
                value = kpp.get("value")
                if isinstance(value, str) and len(value) in (13, 15) and value.isdigit():
                    data = {**data, "kpp": {**kpp, "value": None}}
        return data


class LineItem(SchemaModel):
    name: ExtractedField[str]
    quantity: ExtractedField[Quantity]
    unit: ExtractedField[str]
    unit_price: ExtractedField[UnitPrice]
    amount: ExtractedField[Money]
    vat_rate: ExtractedField[VatRate]
    vat_amount: ExtractedField[Money]


class ExtractedDocument(SchemaModel):
    expected_document_type: ClassVar[DocumentType | None] = None

    document_type: ExtractedField[DocumentType]
    number: ExtractedField[str]
    date: ExtractedField[date]
    supplier: Party
    buyer: Party
    total_amount: ExtractedField[Money]
    vat_amount: ExtractedField[Money]
    currency: ExtractedField[CurrencyCode]
    line_items: list[LineItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_document_type(self) -> Self:
        if (
            self.expected_document_type is not None
            and self.document_type.value is not self.expected_document_type
        ):
            raise ValueError(
                f"для {type(self).__name__} нужен тип {self.expected_document_type.value}"
            )
        return self


class PaymentInvoiceDocument(ExtractedDocument):
    expected_document_type = DocumentType.PAYMENT_INVOICE

    # Срок оплаты может быть в будущем, поэтому NonFutureDate здесь неприменим.
    payment_due_date: ExtractedField[date] | None = None


class VatInvoiceDocument(ExtractedDocument):
    expected_document_type = DocumentType.VAT_INVOICE

    correction_number: ExtractedField[str] | None = None
    correction_date: ExtractedField[NonFutureDate] | None = None


class UniversalTransferDocument(ExtractedDocument):
    expected_document_type = DocumentType.UNIVERSAL_TRANSFER_DOCUMENT

    upd_status: ExtractedField[Literal["1", "2"]]
    operation_name: ExtractedField[str]


class Torg12Document(ExtractedDocument):
    expected_document_type = DocumentType.CONSIGNMENT_NOTE_TORG12

    shipment_date: ExtractedField[NonFutureDate] | None = None
    basis_document: ExtractedField[str] | None = None


class WorkCompletionActDocument(ExtractedDocument):
    expected_document_type = DocumentType.WORK_COMPLETION_ACT

    contract_number: ExtractedField[str] | None = None
    contract_date: ExtractedField[NonFutureDate] | None = None
    service_period_start: ExtractedField[NonFutureDate] | None = None
    service_period_end: ExtractedField[NonFutureDate] | None = None

    @model_validator(mode="after")
    def validate_service_period(self) -> Self:
        start = self.service_period_start.value if self.service_period_start else None
        end = self.service_period_end.value if self.service_period_end else None
        if start is not None and end is not None and end < start:
            raise ValueError("конец периода услуг не может быть раньше начала")
        return self
