from sqlalchemy.dialects.postgresql import JSONB

from docsift.db.models import Base, Document, EvalRun, Extraction, ReviewTask


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "documents",
        "extractions",
        "review_tasks",
        "eval_runs",
    }


def test_extraction_references_document_with_cascade() -> None:
    foreign_key = next(iter(Extraction.__table__.c.document_id.foreign_keys))
    assert foreign_key.target_fullname == "documents.id"
    assert foreign_key.ondelete == "CASCADE"


def test_review_task_references_extraction_with_cascade() -> None:
    foreign_key = next(iter(ReviewTask.__table__.c.extraction_id.foreign_keys))
    assert foreign_key.target_fullname == "extractions.id"
    assert foreign_key.ondelete == "CASCADE"


def test_structured_payloads_use_postgresql_jsonb() -> None:
    assert isinstance(Extraction.__table__.c.result.type, JSONB)
    assert isinstance(ReviewTask.__table__.c.corrected_value.type, JSONB)
    assert isinstance(EvalRun.__table__.c.metrics.type, JSONB)


def test_extraction_has_llm_audit_columns() -> None:
    columns = Extraction.__table__.c
    assert not columns.prompt_text.nullable
    assert not columns.llm_attempts.nullable
    assert {"raw_response", "input_tokens", "output_tokens", "response_time_ms"} <= set(
        columns.keys()
    )


def test_all_tables_have_uuid_and_audit_timestamps() -> None:
    for model in (Document, Extraction, ReviewTask, EvalRun):
        columns = model.__table__.c
        assert columns.id.primary_key
        assert not columns.created_at.nullable
        assert not columns.updated_at.nullable
