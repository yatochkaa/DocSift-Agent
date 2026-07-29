from enum import StrEnum


class DocumentType(StrEnum):
    PAYMENT_INVOICE = "payment_invoice"
    VAT_INVOICE = "vat_invoice"
    UNIVERSAL_TRANSFER_DOCUMENT = "universal_transfer_document"
    CONSIGNMENT_NOTE_TORG12 = "consignment_note_torg12"
    WORK_COMPLETION_ACT = "work_completion_act"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReviewTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class EvalRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class GuardrailRuleCode(StrEnum):
    TOTAL_MISMATCH = "total_mismatch"
    VAT_RATE_MISMATCH = "vat_rate_mismatch"
    INVALID_INN = "invalid_inn"
    SAME_PARTIES = "same_parties"
    DOCUMENT_DATE_OUT_OF_RANGE = "document_date_out_of_range"
    LOW_CONFIDENCE = "low_confidence"

