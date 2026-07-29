from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from docsift.schemas.evals import DatasetManifest, ExpectedDocument

SUPPORTED_DOCUMENT_SUFFIXES = {".pdf", ".csv", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


class DatasetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetSample:
    sample_id: str
    document_path: Path
    expected_path: Path
    expected: ExpectedDocument


@dataclass(frozen=True, slots=True)
class Dataset:
    root: Path
    manifest: DatasetManifest
    samples: tuple[DatasetSample, ...]


def load_dataset(root: str | Path) -> Dataset:
    dataset_root = Path(root).resolve()
    manifest_path = dataset_root / "manifest.json"
    samples_root = dataset_root / "samples"
    if not manifest_path.is_file():
        raise DatasetError(f"Dataset manifest not found: {manifest_path}")
    if not samples_root.is_dir():
        raise DatasetError(f"Dataset samples directory not found: {samples_root}")

    try:
        manifest = DatasetManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise DatasetError(f"Invalid dataset manifest: {manifest_path}") from exc

    samples: list[DatasetSample] = []
    seen_ids: set[str] = set()
    for expected_path in sorted(samples_root.rglob("*.expected.json")):
        sample_id = expected_path.name.removesuffix(".expected.json")
        if sample_id in seen_ids:
            raise DatasetError(f"Duplicate sample id: {sample_id}")
        document_candidates = [
            path
            for path in expected_path.parent.glob(f"{sample_id}.*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_DOCUMENT_SUFFIXES
        ]
        if len(document_candidates) != 1:
            raise DatasetError(
                f"Sample {sample_id} must have exactly one document file, found {len(document_candidates)}"
            )
        try:
            expected = ExpectedDocument.model_validate_json(
                expected_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise DatasetError(f"Invalid expected JSON: {expected_path}") from exc
        samples.append(
            DatasetSample(
                sample_id=sample_id,
                document_path=document_candidates[0],
                expected_path=expected_path,
                expected=expected,
            )
        )
        seen_ids.add(sample_id)

    if not samples:
        raise DatasetError(f"Dataset has no *.expected.json samples: {samples_root}")
    return Dataset(root=dataset_root, manifest=manifest, samples=tuple(samples))

