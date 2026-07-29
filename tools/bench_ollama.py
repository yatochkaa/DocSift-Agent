r"""
bench_ollama.py — ищем, на каком размере промпта Ollama перестаёт укладываться
в таймаут клиента, и сколько стоит structured output (format=JSON schema).

Зависимостей нет — только стандартная библиотека. Запуск:

    .\.venv\Scripts\python.exe tools\bench_ollama.py
    .\.venv\Scripts\python.exe tools\bench_ollama.py --model qwen2.5-coder:3b --num-predict 1024

Что печатает по каждому прогону:
  wall      — реальное время запроса (то, что видит httpx в приложении)
  load      — сколько ушло на загрузку модели в память
  prompt    — токенов на входе / время их обработки
  eval      — токенов сгенерировано / время / токенов в секунду
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:11434"

# Небольшая JSON-схема в духе извлечения реквизитов счёта — чтобы померить
# накладные расходы constrained decoding.
SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string"},
        "number": {"type": "string"},
        "date": {"type": "string"},
        "supplier": {"type": "string"},
        "buyer": {"type": "string"},
        "total": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "price": {"type": "number"},
                    "sum": {"type": "number"},
                },
                "required": ["name", "qty", "price", "sum"],
            },
        },
    },
    "required": ["doc_type", "number", "date", "supplier", "buyer", "total", "items"],
}

FILLER_LINE = (
    "Наименование товара {i}: болт М12х40 оцинкованный, количество {i} шт, "
    "цена 123,45 руб, сумма 1 234,50 руб, НДС не облагается (УСН).\n"
)


def make_document(chars: int) -> str:
    """Синтетический «текст документа» примерно заданной длины."""
    parts = [
        "СЧЁТ № 42 от 15.07.2026\n",
        "Поставщик: ИП Иванов И.И., ИНН 771234567890\n",
        "Покупатель: ООО «СеверТрейд», ИНН 7812345678\n",
    ]
    i = 1
    size = sum(len(p) for p in parts)
    while size < chars:
        line = FILLER_LINE.format(i=i)
        parts.append(line)
        size += len(line)
        i += 1
    return "".join(parts)


def call_ollama(
    base_url: str,
    model: str,
    document: str,
    *,
    use_format: bool,
    num_predict: int,
    num_ctx: int,
    timeout: float,
) -> dict:
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
        },
        "messages": [
            {
                "role": "system",
                "content": "Ты извлекаешь реквизиты из русских первичных документов. Отвечай только JSON.",
            },
            {
                "role": "user",
                "content": "Извлеки реквизиты и позиции из документа:\n\n" + document,
            },
        ],
    }
    if use_format:
        payload["format"] = SCHEMA

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        wall = time.perf_counter() - started
        return {"ok": True, "wall": wall, "body": body}
    except Exception as exc:  # noqa: BLE001 — нам нужен любой отказ как результат замера
        wall = time.perf_counter() - started
        return {"ok": False, "wall": wall, "error": f"{type(exc).__name__}: {exc}"}


def ns_to_s(value) -> float:
    try:
        return float(value) / 1e9
    except (TypeError, ValueError):
        return 0.0


def report(label: str, result: dict) -> None:
    if not result["ok"]:
        print(f"{label:<34} FAIL after {result['wall']:7.1f}s  {result['error']}")
        return

    b = result["body"]
    load_s = ns_to_s(b.get("load_duration"))
    prompt_n = b.get("prompt_eval_count") or 0
    prompt_s = ns_to_s(b.get("prompt_eval_duration"))
    eval_n = b.get("eval_count") or 0
    eval_s = ns_to_s(b.get("eval_duration"))
    tps = (eval_n / eval_s) if eval_s else 0.0
    content = (b.get("message") or {}).get("content") or ""

    print(
        f"{label:<34} wall {result['wall']:7.1f}s | load {load_s:5.1f}s | "
        f"prompt {prompt_n:6d} tok / {prompt_s:6.1f}s | "
        f"eval {eval_n:5d} tok / {eval_s:6.1f}s ({tps:5.1f} tok/s) | "
        f"out {len(content):5d} chars"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--model", default="qwen2.5-coder:3b")
    ap.add_argument("--num-predict", type=int, default=1024)
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument(
        "--sizes",
        default="500,2000,8000,20000,40000",
        help="размеры синтетического документа в символах, через запятую",
    )
    ap.add_argument(
        "--file",
        help="вместо синтетики взять текст из файла (например, дамп текста реального PDF)",
    )
    args = ap.parse_args()

    print(f"model={args.model} num_predict={args.num_predict} num_ctx={args.num_ctx}")
    print("-" * 118)

    # Прогрев: первый вызов платит за загрузку модели, его в статистику не берём.
    warm = call_ollama(
        args.base_url,
        args.model,
        "ок",
        use_format=False,
        num_predict=8,
        num_ctx=args.num_ctx,
        timeout=args.timeout,
    )
    report("warmup (модель в память)", warm)
    if not warm["ok"]:
        print("\nOllama недоступна или падает уже на прогреве — дальше бенчить нечего.")
        return
    print("-" * 118)

    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        cases = [(f"file:{len(text)}ch", text)]
    else:
        cases = []
        for raw in args.sizes.split(","):
            raw = raw.strip()
            if not raw:
                continue
            n = int(raw)
            cases.append((f"{n}ch", make_document(n)))

    for name, document in cases:
        for use_format in (False, True):
            mode = "schema" if use_format else "plain "
            result = call_ollama(
                args.base_url,
                args.model,
                document,
                use_format=use_format,
                num_predict=args.num_predict,
                num_ctx=args.num_ctx,
                timeout=args.timeout,
            )
            report(f"{name:>10} / {mode}", result)

    print("-" * 118)
    print(
        "Читать так: если wall на реальном размере документа больше read-таймаута\n"
        "httpx в providers.py — это и есть причина ReadTimeout. Если строка 'schema'\n"
        "кардинально медленнее 'plain' — виновата JSON-схема, а не размер входа."
    )


if __name__ == "__main__":
    main()
