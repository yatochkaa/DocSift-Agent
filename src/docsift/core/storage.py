from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import UploadFile

from docsift.domain.exceptions import FileTooLargeError


@dataclass(frozen=True, slots=True)
class StoredObject:
    object_key: str
    size_bytes: int
    sha256: str


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    async def put(
        self,
        upload: UploadFile,
        object_key: str,
        max_bytes: int,
        chunk_bytes: int,
    ) -> StoredObject:
        destination = self._root / object_key
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        await anyio.Path(destination.parent).mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256()
        size = 0
        try:
            async with await anyio.open_file(temporary, "wb") as output:
                while chunk := await upload.read(chunk_bytes):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileTooLargeError
                    digest.update(chunk)
                    await output.write(chunk)
            if size == 0:
                raise FileTooLargeError
            await anyio.to_thread.run_sync(os.replace, temporary, destination)
        except Exception:
            try:
                await anyio.Path(temporary).unlink()
            except FileNotFoundError:
                pass
            raise

        return StoredObject(object_key=object_key, size_bytes=size, sha256=digest.hexdigest())

    async def delete(self, object_key: str) -> None:
        try:
            await anyio.Path(self._root / object_key).unlink()
        except FileNotFoundError:
            pass

