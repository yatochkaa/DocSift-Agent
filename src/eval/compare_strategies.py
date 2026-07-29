from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from docsift.services.evals.strategy import (
    StrategyEnvelope,
    compare_strategies,
    load_strategy_report,
    render_strategy_comparison_table,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Сравнить стратегии извлечения по JSON-отчётам eval.",
    )
    parser.add_argument(
        "reports",
        nargs="+",
        type=Path,
        help="Пути к JSON-отчётам стратегий (минимум один).",
    )
    return parser


def execute(report_paths: list[Path]) -> None:
    envelopes: list[StrategyEnvelope] = []
    for path in report_paths:
        envelopes.append(load_strategy_report(path))
    comparison = compare_strategies(envelopes)
    print(render_strategy_comparison_table(comparison))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        execute(args.reports)
    except Exception as exc:  # noqa: BLE001 - CLI must return a controlled error
        print(f"Ошибка сравнения стратегий: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())