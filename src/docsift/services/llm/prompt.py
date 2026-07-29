from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True, slots=True)
class VersionedPrompt:
    version: str
    text: str


def load_document_extraction_prompt(version: str) -> VersionedPrompt:
    if not version or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for character in version):
        raise ValueError("Invalid prompt version")
    resource = files("docsift.prompts.document_extraction").joinpath(f"{version}.md")
    if not resource.is_file():
        raise ValueError(f"Unknown document extraction prompt version: {version}")
    return VersionedPrompt(version=version, text=resource.read_text(encoding="utf-8"))
