"""Вызовы форжа для автоперезапуска CI на свежей базе (#1956).

Примесь ``GitOpsIntegration``: файл ``git_ops.py`` стоит у потолка размера
(docs/agent-context/file-size-budget.json), поэтому новая тема живёт рядом.
Роутинг тот же, что у остальных вопросов к форжу: кто умеет - отвечает, кто не
умеет - говорит ``unsupported`` или ``None``.
"""

from __future__ import annotations

from typing import Any

from hub.integrations.protocols import CIRunRequestResult, ForgePlugin


class RerunCalls:
    def _forge_for(self, forge: str = "") -> ForgePlugin:  # переопределяет класс-хозяин
        raise NotImplementedError

    async def rerun_failed_jobs(
        self,
        run_id: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> CIRunRequestResult:
        """Просьба форжу перезапустить упавшие job прогона (#1956, платный POST)."""
        return await self._forge_for(forge).rerun_failed_jobs(
            run_id, repo=repo, gh_repo=gh_repo
        )

    async def ci_attempt_jobs(
        self,
        run_id: str,
        attempt: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[dict[str, Any]] | None:
        """Имена и итоги job одной попытки прогона (#1956); None - не прочиталось."""
        return await self._forge_for(forge).ci_attempt_jobs(
            run_id, attempt, repo=repo, gh_repo=gh_repo
        )
