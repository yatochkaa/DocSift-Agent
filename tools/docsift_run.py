r"""DocSift: one-shot diagnostics + synchronous ingest.

This single script replaces the whole command ping-pong. It:
  1. loads .env and prints the effective LLM settings;
  2. checks that the configured model exists in Ollama and warms it up;
  3. introspects ingest_document() so no argument guessing is needed;
  4. runs the full pipeline SYNCHRONOUSLY (background=False) for each PDF,
     one at a time, with timing;
  5. prints the resulting rows from Postgres (no docker/psql needed);
  6. prints a full traceback if anything explodes.

Usage (from the project root, with the venv python):

    .\.venv\Scripts\python.exe tools\docsift_run.py
    .\.venv\Scripts\python.exe tools\docsift_run.py --diagnose-only
    .\.venv\Scripts\python.exe tools\docsift_run.py --all
    .\.venv\Scripts\python.exe tools\docsift_run.py path\to\file.pdf
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SAMPLES = ROOT / "datasets" / "accounting" / "v1" / "samples"
DEFAULT_DOC = "doc_02_schet_ip_usn"


def line(title: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(title, flush=True)
    print("=" * 72, flush=True)


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        print("!! .env not found at", path, flush=True)
        return
    raw = path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for item in raw.splitlines():
        item = item.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())
        count += 1
    print(f"loaded {count} vars from .env", flush=True)


def http_json(url: str, payload: dict | None = None, timeout: float = 300.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def show_settings():
    line("1. SETTINGS")
    from docsift.core.config import get_settings

    settings = get_settings()
    keys = [k for k in dir(settings) if k.startswith("llm_")]
    for key in sorted(keys):
        try:
            print(f"  settings.{key} = {getattr(settings, key)!r}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  settings.{key} -> <error {exc}>", flush=True)
    for extra in ("database_url", "storage_path"):
        if hasattr(settings, extra):
            print(f"  settings.{extra} = {getattr(settings, extra)!r}", flush=True)
    return settings


def check_ollama(settings) -> None:
    line("2. OLLAMA")
    base = str(getattr(settings, "llm_base_url", "http://localhost:11434")).rstrip("/")
    model = str(getattr(settings, "llm_model", ""))
    print(f"  base_url = {base}", flush=True)
    print(f"  model    = {model}", flush=True)
    try:
        tags = http_json(f"{base}/api/tags", timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"  !! /api/tags failed: {type(exc).__name__}: {exc}", flush=True)
        return
    names = [m.get("name", "") for m in tags.get("models", [])]
    print("  installed:", ", ".join(names) or "<none>", flush=True)
    if model not in names:
        print(f"  !! MODEL '{model}' IS NOT INSTALLED -> every request will fail", flush=True)
        return
    print("  model is installed, warming up ...", flush=True)
    started = time.perf_counter()
    try:
        result = http_json(
            f"{base}/api/chat",
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "keep_alive": "30m",
                "options": {"num_predict": 8, "temperature": 0},
            },
            timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  !! warmup failed: {type(exc).__name__}: {exc}", flush=True)
        return
    elapsed = time.perf_counter() - started
    load_ns = result.get("load_duration") or 0
    print(f"  warmup ok in {elapsed:.2f}s (model load {load_ns / 1e9:.2f}s)", flush=True)


def show_signature():
    line("3. ingest_document SIGNATURE")
    from docsift.pipeline.ingest import ingest_document

    sig = inspect.signature(ingest_document)
    for name, param in sig.parameters.items():
        default = "<required>" if param.default is inspect._empty else repr(param.default)
        print(f"  {name}: default={default}", flush=True)
    return ingest_document, sig


def resolve_targets(args) -> list[Path]:
    if args.files:
        return [Path(item) for item in args.files]
    if args.all:
        return sorted(SAMPLES.glob("doc_*/doc_*.pdf"))
    candidate = SAMPLES / DEFAULT_DOC / f"{DEFAULT_DOC}.pdf"
    return [candidate]


async def run(args) -> int:
    load_dotenv()
    settings = show_settings()
    check_ollama(settings)
    ingest_document, sig = show_signature()

    from docsift.db.session import build_engine, build_session_factory
    from sqlalchemy import text

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    failures = 0

    async def report_one(document_id: str) -> None:
        """Print the stored status and error message for a single document."""
        query = text(
            "select d.status, e.status as extraction_status, e.error_message"
            " from documents d"
            " left join extractions e on e.document_id = d.id"
            " where d.id = cast(:doc_id as uuid)"
        )
        try:
            async with session_factory() as session:
                row = (await session.execute(query, {"doc_id": document_id})).mappings().first()
        except Exception as exc:  # noqa: BLE001
            print(f"     (db lookup failed: {type(exc).__name__}: {exc})", flush=True)
            return
        if row is None:
            print("     (no db row found)", flush=True)
            return
        print(
            f"     db: doc={row['status']} extraction={row['extraction_status']}",
            flush=True,
        )
        if row["error_message"]:
            print(f"     err: {row['error_message']}", flush=True)

    try:
        if not args.diagnose_only:
            targets = resolve_targets(args)
            line(f"4. INGEST ({len(targets)} file(s), sequential, background=False)")
            for path in targets:
                if not path.exists():
                    print(f"  !! missing file: {path}", flush=True)
                    failures += 1
                    continue
                payload = path.read_bytes()
                candidates = {
                    "file_name": path.name,
                    "payload": payload,
                    "content": payload,
                    "data": payload,
                    "session_factory": session_factory,
                    "settings": settings,
                    "background": False,
                }
                kwargs = {k: v for k, v in candidates.items() if k in sig.parameters}
                missing = [
                    name
                    for name, param in sig.parameters.items()
                    if param.default is inspect._empty and name not in kwargs
                ]
                if missing:
                    print(f"  !! cannot call ingest_document, unmapped params: {missing}", flush=True)
                    return 2
                print(f"\n  -> {path.name} ({len(payload)} bytes)", flush=True)
                print(f"     kwargs: {sorted(kwargs)}", flush=True)
                started = time.perf_counter()
                try:
                    result = await ingest_document(**kwargs)
                    elapsed = time.perf_counter() - started
                    print(f"     result after {elapsed:.1f}s: {result}", flush=True)
                    if isinstance(result, dict) and result.get("already_existed"):
                        stamp = int(time.time())
                        # Dedupe is based on the stored object key (content hash),
                        # so append an ignorable PDF comment to force a new row.
                        fresh_payload = payload + f"\n% docsift-rerun {stamp}\n".encode()
                        kwargs["payload"] = fresh_payload
                        kwargs["file_name"] = f"{path.stem}__rerun{stamp}{path.suffix}"
                        print(
                            "     already existed -> retrying as "
                            f"{kwargs['file_name']} ({len(fresh_payload)} bytes)",
                            flush=True,
                        )
                        started = time.perf_counter()
                        result = await ingest_document(**kwargs)
                        elapsed = time.perf_counter() - started
                        print(f"     result after {elapsed:.1f}s: {result}", flush=True)
                    if isinstance(result, dict) and result.get("id"):
                        await report_one(str(result["id"]))
                    if isinstance(result, dict) and str(result.get("status", "")).lower() in {
                        "failed",
                        "error",
                    }:
                        failures += 1
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.perf_counter() - started
                    failures += 1
                    print(f"     FAILED after {elapsed:.1f}s: {type(exc).__name__}: {exc}", flush=True)
                    traceback.print_exc()

        line("5. DATABASE (latest 10 documents)")
        query = text(
            "select d.original_filename, d.status, d.created_at,"
            " e.status as extraction_status, e.error_message"
            " from documents d"
            " left join extractions e on e.document_id = d.id"
            " order by d.created_at desc limit 10"
        )
        async with session_factory() as session:
            rows = (await session.execute(query)).mappings().all()
        if not rows:
            print("  <no documents>", flush=True)
        for row in rows:
            print(
                "  {0} | doc={1} | extraction={2} | {3}\n      err: {4}".format(
                    row["created_at"],
                    row["status"],
                    row["extraction_status"],
                    row["original_filename"],
                    row["error_message"],
                ),
                flush=True,
            )
    finally:
        await engine.dispose()

    line("DONE" if not failures else f"DONE WITH {failures} FAILURE(S)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DocSift diagnostics + sync ingest")
    parser.add_argument("files", nargs="*", help="PDF paths; default is doc_02")
    parser.add_argument("--all", action="store_true", help="process every sample PDF")
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="only settings/ollama/signature/db, no ingest",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
