from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from docsift.db.models import EvalRun


class EvalRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, eval_run: EvalRun) -> EvalRun:
        self._session.add(eval_run)
        await self._commit(eval_run)
        return eval_run

    async def update(self, eval_run: EvalRun) -> EvalRun:
        await self._commit(eval_run)
        return eval_run

    async def _commit(self, eval_run: EvalRun) -> None:
        try:
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        await self._session.refresh(eval_run)
