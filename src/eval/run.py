from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from docsift.core.config import EvalProfileName, ExtractionStrategy, Settings
from docsift.db.session import build_engine, build_session_factory
from docsift.repositories.eval_runs import EvalRunRepository
from docsift.schemas.evals import EvalPricing
from docsift.services.evals import load_dataset, render_metrics_table
from docsift.services.evals.runner import EvalRunner
from docsift.services.evals.strategy import StrategyEnvelope, save_strategy_report
from docsift.services.llm import build_eval_llm_provider
from docsift.services.llm.cache import DiskExtractionCache
from docsift.services.text_extraction import TextExtractionService

DEFAULT_DATASET_PATH = Path("datasets/accounting/v1")
DEFAULT_REPORT_DIRECTORY = Path("var/eval-reports")
DEFAULT_CACHE_DIRECTORY = Path("var/llm-cache")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Запустить eval извлечения на размеченном датасете.",
    )
    parser.add_argument(
        "--provider",
        choices=("local", "cloud"),
        default="local",
        help="Профиль LLM из конфигурации (по умолчанию: local).",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Каталог датасета (по умолчанию: {DEFAULT_DATASET_PATH}).",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="Обработать только первые N документов.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Путь итогового JSON-отчёта.",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="Версия промта; по умолчанию берётся из DOCSIFT_LLM_PROMPT_VERSION.",
    )
    parser.add_argument(
        "--strategy",
        choices=("cheap_only", "expensive_only", "cascade"),
        default="cheap_only",
        help="Стратегия извлечения (по умолчанию: cheap_only).",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        default=False,
        help="Использовать кеш LLM (по умолчанию кеш обходится для чистоты eval).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIRECTORY,
        help="Каталог дискового кеша ответов LLM (по умолчанию: var/llm-cache).",
    )
    parser.add_argument(
        "--dump-raw",
        action="store_true",
        default=False,
        help="Сохранять сырые extracted/expected документы в отчёте по каждому образцу.",
    )
    return parser


def default_report_path(strategy: str, dataset_name: str, dataset_version: str, run_id) -> Path:
    filename = (
        f"{dataset_name}-{dataset_version}-{strategy}-"
        f"{run_id}.json"
    )
    return DEFAULT_REPORT_DIRECTORY / filename


def print_progress(completed: int, total: int) -> None:
    print(f"Обработано {completed} из {total}", flush=True)


def render_step_durations(
    step_durations: dict[str, float],
    total_duration_seconds: float,
) -> str:
    """Строка вида ``llm_extraction=367.100s (99%)`` по убыванию длительности."""
    if not step_durations:
        return "Шаги: замеров нет"
    measured = sum(step_durations.values())
    parts = []
    for step, seconds in sorted(step_durations.items(), key=lambda item: -item[1]):
        share = f" ({seconds / total_duration_seconds * 100:.0f}%)" if total_duration_seconds else ""
        parts.append(f"{step}={seconds:.3f}s{share}")
    unmeasured = total_duration_seconds - measured
    if total_duration_seconds and unmeasured > 0.0005:
        parts.append(f"прочее={unmeasured:.3f}s ({unmeasured / total_duration_seconds * 100:.0f}%)")
    return "Шаги: " + "; ".join(parts)


async def execute(args: argparse.Namespace) -> Path:
    settings = Settings()
    strategy = cast(ExtractionStrategy, args.strategy)
    profile = cast(EvalProfileName, args.provider)
    profile_config = settings.eval_profile(profile)
    provider = build_eval_llm_provider(settings, profile)
    pricing = EvalPricing(
        input_price_per_million=profile_config.input_price_per_million,
        output_price_per_million=profile_config.output_price_per_million,
    )

    expensive_provider = None
    expensive_pricing = None
    if strategy == "cascade":
        cloud_config = settings.eval_profile("cloud")
        expensive_provider = build_eval_llm_provider(settings, "cloud")
        expensive_pricing = EvalPricing(
            input_price_per_million=cloud_config.input_price_per_million,
            output_price_per_million=cloud_config.output_price_per_million,
        )
    if strategy == "expensive_only":
        provider = build_eval_llm_provider(settings, "cloud")
        pricing = EvalPricing(
            input_price_per_million=settings.eval_cloud_input_price_per_million,
            output_price_per_million=settings.eval_cloud_output_price_per_million,
        )

    dataset = load_dataset(args.dataset)
    cache = DiskExtractionCache(args.cache_dir) if args.use_cache else None
    engine = build_engine(settings)
    try:
        session_factory = build_session_factory(engine)
        async with session_factory() as session:
            runner = EvalRunner(
                provider=provider,
                provider_profile=profile,
                pricing=pricing,
                prompt_version=args.prompt_version or settings.llm_prompt_version,
                text_extractor=TextExtractionService(),
                run_repository=EvalRunRepository(session),
                progress_callback=print_progress,
                strategy=strategy,
                bypass_cache=not args.use_cache,
                expensive_provider=expensive_provider,
                expensive_pricing=expensive_pricing,
                confidence_threshold=settings.cascade_confidence_threshold,
                dump_raw=args.dump_raw,
                cache=cache,
            )
            report = await runner.run(dataset, limit=args.limit)
    finally:
        await engine.dispose()

    envelope = StrategyEnvelope(strategy=strategy, report=report)
    output_path = args.output or default_report_path(
        strategy, report.dataset_name, report.dataset_version, report.run_id
    )
    save_strategy_report(envelope, output_path)
    print(
        f"Стратегия: {strategy}; Провайдер: {report.provider_backend}/{report.model}; "
        f"prompt={report.prompt_version}; temperature={report.temperature}"
    )
    print(
        f"Документы: {report.succeeded_count} успешно, {report.failed_count} с ошибкой; "
        f"время={report.total_duration_seconds:.3f}s; "
        f"среднее={report.average_duration_seconds:.3f}s/документ; "
        f"стоимость={report.cost_usd if report.cost_usd is not None else 'неизвестна'} USD"
    )
    print(render_step_durations(report.step_duration_totals, report.total_duration_seconds))
    if cache is not None:
        stats = cache.stats()
        print(
            f"Кеш: попаданий={stats['hits']}; промахов={stats['misses']}; "
            f"записей={stats['entries']}; каталог={stats['directory']}"
        )
    print(render_metrics_table(report))
    print(f"Отчёт сохранён: {output_path}")
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(execute(args))
    except Exception as exc:  # noqa: BLE001 - CLI must return a controlled error
        print(f"Ошибка eval-прогона: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

