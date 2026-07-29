from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from docsift.services.evals import (
    compare_reports,
    load_report,
    render_comparison_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сравнить два JSON-отчёта eval пополево.",
    )
    parser.add_argument("run_a", type=Path, help="Базовый JSON-отчёт.")
    parser.add_argument("run_b", type=Path, help="Новый JSON-отчёт.")
    return parser


def execute(run_a_path: Path, run_b_path: Path) -> None:
    run_a = load_report(run_a_path)
    run_b = load_report(run_b_path)
    comparison = compare_reports(run_a, run_b)
    print(render_comparison_table(comparison))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        execute(args.run_a, args.run_b)
    except Exception as exc:  # noqa: BLE001 - CLI must return a controlled error
        print(f"Ошибка сравнения eval-отчётов: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
