"""Адаптеры форжей — хостингов репозиториев (#1113).

Каждый модуль здесь реализует ``protocols.ForgePlugin`` для одного форжа.
Хаб-ядро и ``git_ops`` знают только протокол.
"""

from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge

#: Адрес в вебе по ИМЕНИ форжа (#1119).
#:
#: Отдельно от выбора адаптера для вызовов (#1146) намеренно: ссылка — это
#: чистое форматирование, ей не нужны ни токен, ни сеть, ни состояние. Связать
#: её с выбором адаптера значило бы отложить #1119 до #1146 без всякой пользы.
#: Имя форжа при этом приходит из ЕДИНСТВЕННОГО читателя — project_policy
#: (#1114), так что второй копии правила «какой форж» здесь не заводится.
_BY_NAME: dict[str, type] = {"github": GitHubForge, "gitverse": GitVerseForge}


def repo_url(forge: str, gh_repo: str) -> str:
    """Адрес репозитория в вебе. Незнакомый форж — как github, см. #1114."""
    return _BY_NAME.get(forge, GitHubForge)().repo_url(gh_repo)


def pr_url(forge: str, gh_repo: str, pr_number: int) -> str:
    """Адрес запроса на слияние. У GitHub /pull/, у GitVerse /pulls/."""
    return _BY_NAME.get(forge, GitHubForge)().pr_url(pr_number, gh_repo)


__all__ = ["GitHubForge", "GitVerseForge", "pr_url", "repo_url"]
