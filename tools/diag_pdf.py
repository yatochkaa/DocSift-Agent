r"""
diag_pdf.py — есть ли у тестовых PDF текстовый слой, и сколько текста уйдёт в LLM.

Ничего не ставит: перебирает те PDF-библиотеки, которые уже есть в venv
(pdfplumber / PyMuPDF / pypdf / pdfminer.six) и берёт первую доступную.
Если ни одной нет — скажет, что поставить.

Запуск (директорию с тестовыми файлами подставь свою):

    .\.venv\Scripts\python.exe tools\diag_pdf.py var\uploads
    .\.venv\Scripts\python.exe tools\diag_pdf.py tests\fixtures --dump var\text

--dump сохраняет извлечённый текст в .txt, чтобы потом скормить его
bench_ollama.py --file и померить реальное время на реальном тексте.
"""

from __future__ import annotations

import argparse
import os
import sys


def pick_backend():
    try:
        import pdfplumber  # noqa: F401

        def extract(path):
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                return [(p.extract_text() or "") for p in pdf.pages]

        return "pdfplumber", extract
    except ImportError:
        pass

    try:
        import fitz  # noqa: F401  (PyMuPDF)

        def extract(path):
            import fitz

            doc = fitz.open(path)
            try:
                return [page.get_text() or "" for page in doc]
            finally:
                doc.close()

        return "PyMuPDF", extract
    except ImportError:
        pass

    try:
        from pypdf import PdfReader  # noqa: F401

        def extract(path):
            from pypdf import PdfReader

            reader = PdfReader(path)
            return [(page.extract_text() or "") for page in reader.pages]

        return "pypdf", extract
    except ImportError:
        pass

    try:
        from pdfminer.high_level import extract_text  # noqa: F401

        def extract(path):
            from pdfminer.high_level import extract_text

            return [extract_text(path) or ""]

        return "pdfminer.six (постранично не умеет)", extract
    except ImportError:
        pass

    return None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", help="папка с PDF (рекурсивно)")
    ap.add_argument("--dump", help="куда сложить извлечённый текст в .txt")
    args = ap.parse_args()

    name, extract = pick_backend()
    if extract is None:
        print(
            "Ни одной PDF-библиотеки в venv не найдено.\n"
            "Поставь одну: .\\.venv\\Scripts\\python.exe -m pip install pdfplumber\n"
            "(или посмотри, чем PDF читает сам DocSift, и запусти скрипт с тем же venv)"
        )
        sys.exit(1)

    print(f"backend: {name}\n")

    if args.dump:
        os.makedirs(args.dump, exist_ok=True)

    pdfs = []
    for root, _dirs, files in os.walk(args.directory):
        for fn in files:
            if fn.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, fn))

    if not pdfs:
        print(f"В {args.directory} PDF не найдено.")
        sys.exit(1)

    for path in sorted(pdfs):
        try:
            pages = extract(path)
        except Exception as exc:  # noqa: BLE001
            print(f"{os.path.basename(path):<36} ОШИБКА ЧТЕНИЯ: {type(exc).__name__}: {exc}")
            continue

        total = sum(len(p) for p in pages)
        per_page = ", ".join(str(len(p)) for p in pages)
        verdict = "СКАН / нет текстового слоя -> нужен OCR" if total < 200 else "текстовый слой есть"
        print(f"{os.path.basename(path):<36} стр: {len(pages):>2} | символов: {total:>7} | {verdict}")
        print(f"{'':<36} по страницам: {per_page}")
        head = (pages[0] if pages else "")[:200].replace("\n", " \\n ")
        print(f"{'':<36} начало: {head!r}\n")

        if args.dump:
            out = os.path.join(
                args.dump, os.path.splitext(os.path.basename(path))[0] + ".txt"
            )
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("\n\n=== PAGE BREAK ===\n\n".join(pages))

    print(
        "Вывод: документы с 'СКАН' в текстовую модель qwen2.5-coder отдавать нельзя —\n"
        "это отдельная задача (OCR или vision-модель), а не таймаут.\n"
        "Для остальных возьми самый большой .txt из --dump и прогони:\n"
        "  .\\.venv\\Scripts\\python.exe tools\\bench_ollama.py --file var\\text\\doc_03_torg12_severtrade.txt"
    )


if __name__ == "__main__":
    main()
