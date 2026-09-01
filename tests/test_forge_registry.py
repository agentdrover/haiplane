"""Форж как объявленный плагин: контракт, регистрация, поведение без него (#1113)."""

from __future__ import annotations

import inspect

import pytest

from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge
from hub.integrations.noop import NoopForge
from hub.integrations.protocols import (
    CIProbeOutcome,
    ForgePlugin,
    MergeabilityOutcome,
)
from hub.integrations.registry import PluginRegistry


def _declared_on(cls) -> set[str]:
    """Публичные операции, объявленные прямо на классе (без наследованного)."""
    return {
        name
        for name, value in vars(cls).items()
        if not name.startswith("_") and (inspect.isfunction(value))
    }


def test_forge_protocol_covers_every_implementation_method():
    """AC-3. Реализация не имеет метода, которого нет в контракте.

    Это защита от повторения #847: у ``GitOpsPlugin`` реализации обзавелись
    читателями git и GitHub, которых протокол не объявлял, и подменить плагин
    можно было только угадав, что ещё требуется сверх написанного. Пропажу
    такого метода не ловил никто — контракт о нём не знал.

    Проверка односторонняя намеренно. Метод протокола, которого нет у
    реализации, поймает mypy и первый же вызов; метод реализации, которого нет
    в протоколе, не поймает никто — именно он и есть тихий уход контракта.
    """
    declared = _declared_on(ForgePlugin)
    for impl in (GitHubForge, GitVerseForge, NoopForge):
        undeclared = _declared_on(impl) - declared
        assert not undeclared, (
            f"{impl.__name__} несёт операции, которых нет в ForgePlugin: "
            f"{sorted(undeclared)}. Либо объявите их в протоколе, либо сделайте "
            f"приватными — иначе второй адаптер обязан их угадать."
        )


def test_both_forge_implementations_answer_the_whole_protocol():
    """Ни одна реализация не отстаёт от контракта на метод."""
    declared = _declared_on(ForgePlugin)
    for impl in (GitHubForge, GitVerseForge, NoopForge):
        missing = declared - _declared_on(impl)
        assert not missing, f"{impl.__name__} не реализует: {sorted(missing)}"


def test_registry_starts_with_a_noop_forge():
    """AC-2. Свежий реестр несёт форж, а не None."""
    registry = PluginRegistry()
    assert isinstance(registry.forge, NoopForge)
    assert isinstance(registry.forge, ForgePlugin)


@pytest.mark.asyncio
async def test_hub_starts_with_noop_forge():
    """AC-2. Без настроенного форжа ни один вызов не падает исключением.

    И, что важнее кода возврата: ни один не отвечает содержательно. «Спросить
    не удалось» и «ответ отрицательный» — разные факты, и заглушка, которая их
    смешивает, заставила бы гейт принять решение по молчанию (#419, #725).
    """
    forge = NoopForge()

    assert await forge.create_pr("t", "b", "br", "develop") is None
    assert await forge.pr_for_branch("br") is None
    assert await forge.open_or_update_pr("develop", "release", "t", "b") is None
    # Не "closed" и не "absent": пустая строка — это «посмотреть не смогли».
    assert await forge.pr_state(1) == ""
    assert await forge.pr_is_draft(1) is False
    assert await forge.mark_pr_ready(1) is False
    assert await forge.pr_head_sha(1) == ""
    assert await forge.pr_refs(1) == ("", "")
    assert (await forge.pr_mergeability(1))[0] is MergeabilityOutcome.unavailable
    assert await forge.merge_commit_sha(1) == ""
    assert await forge.merge_pr(1, "subject") is False
    assert (await forge.check_pr_ci(1)).outcome is CIProbeOutcome.unavailable
    # None, а не []: «прогонов нет» и «спросить не смогли» ведут к
    # противоположным выводам о том, зелёная ли база.
    assert await forge.branch_ci_runs("develop") is None
    assert await forge.ci_failure_logs(1, "br") == {
        "failed_checks": [],
        "log_summary": "",
        "run_url": "",
    }
    assert await forge.has_workflows() is None
    assert await forge.compare_subjects("develop", "main") == []
    assert (await forge.merge_branches("develop", "main", "msg"))[0] == "unavailable"


def test_every_forge_names_itself():
    """Отказ должен называть, КТО отказал, а не только что отказали."""
    assert GitHubForge().name == "github"
    assert GitVerseForge().name == "gitverse"
    assert NoopForge().name == "noop"
