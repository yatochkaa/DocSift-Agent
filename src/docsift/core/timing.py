"""Замер длительности шагов конвейера извлечения.

Главное свойство: шаг попадает в замер даже тогда, когда внутри него возникло
исключение. Именно аварийный прогон и надо уметь читать: по набору ключей
видно, на каком шаге документ встал.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import perf_counter

#: Стабильные имена шагов. Строки уезжают в отчёт, поэтому менять их нельзя
#: без поднятия report_version.
STEP_TEXT_EXTRACTION = "text_extraction"
STEP_LLM_EXTRACTION = "llm_extraction"
STEP_METRICS = "metrics"


class StepTimings:
    """Накапливает длительности шагов в порядке первого запуска."""

    __slots__ = ("_durations",)

    def __init__(self) -> None:
        self._durations: dict[str, float] = {}

    @contextmanager
    def measure(self, step: str) -> Iterator[None]:
        """Замерить блок как шаг ``step``."""
        started = perf_counter()
        try:
            yield
        finally:
            self.add(step, perf_counter() - started)

    def add(self, step: str, seconds: float) -> None:
        """Добавить длительность к шагу; повторные вызовы суммируются."""
        if not step or not step.strip():
            raise ValueError("Step name must not be empty")
        # perf_counter монотонен, но отрицательное значение сломало бы валидацию схемы.
        self._durations[step] = self._durations.get(step, 0.0) + max(0.0, float(seconds))

    def as_dict(self) -> dict[str, float]:
        """Копия замеров, округлённых до микросекунд."""
        return {step: round(seconds, 6) for step, seconds in self._durations.items()}

    def __len__(self) -> int:
        return len(self._durations)

    def __contains__(self, step: object) -> bool:
        return step in self._durations


def merge_step_durations(
    total: dict[str, float],
    addition: Mapping[str, float],
) -> dict[str, float]:
    """Сложить замеры одного документа с накопленными по прогону (на месте)."""
    for step, seconds in addition.items():
        total[step] = round(total.get(step, 0.0) + seconds, 6)
    return total
