"""Адаптеры форжей — хостингов репозиториев (#1113).

Каждый модуль здесь реализует ``protocols.ForgePlugin`` для одного форжа.
Хаб-ядро и ``git_ops`` знают только протокол.
"""

from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge
from hub.models import DEFAULT_FORGE

#: Git-хост по ИМЕНИ форжа (#1118).
#:
#: Отдельно от выбора адаптера для вызовов API (#1146) намеренно: адрес клона
#: — чистое форматирование, ему не нужны ни токен, ни сеть, ни состояние.
#: Отдельно и от адреса API: у GitVerse это разные точки входа
#: (``api.gitverse.ru`` против ``gitverse.ru``) и разные credential'ы —
#: токен для API, deploy key для git.
_GIT_HOSTS: dict[str, str] = {"github": "github.com", "gitverse": "gitverse.ru"}


#: Адаптер по имени форжа. Реестр гейтовых вызовов — задача #1146; здесь он
#: нужен провижинингу, которое обязано спросить ИМЕННО тот форж, что объявлен
#: у проекта: спросить не тот значит вернуться к дефекту #1118 с другой
#: стороны. Когда #1146 заведёт свой резолвер, он заменит эту функцию, а не
#: встанет рядом.
_CLIENTS: dict[str, type] = {"github": GitHubForge, "gitverse": GitVerseForge}


def client_for(forge: str):
    """Адаптер объявленного форжа. Незнакомое имя — github (#1114)."""
    return _CLIENTS.get(forge, _CLIENTS[DEFAULT_FORGE])()


def git_host(forge: str) -> str:
    """Хост, на котором лежит git этого форжа.

    Незнакомое имя сводится к github — то же умолчание и по той же причине,
    что у единственного читателя форжа (#1114): неизвестного форжа не бывает,
    бывает необъявленный.
    """
    return _GIT_HOSTS.get(forge, _GIT_HOSTS[DEFAULT_FORGE])


def forge_of_host(host: str) -> str:
    """Имя форжа по git-хосту, или "" для незнакомого хоста.

    Пустая строка означает «не знаем», а НЕ «чужой». Самоподнятый git,
    зеркало на своём домене, локальный путь в тесте не принадлежат ни одному
    объявленному форжу, и обвинять их не в чем: «не смог посмотреть» и «ответ
    отрицательный» — разные исходы (#725). Поэтому сверка происхождения клона
    отказывает только тогда, когда УЗНАЛА чужой хост.
    """
    host = host.strip().lower()
    for name, known in _GIT_HOSTS.items():
        if host == known:
            return name
    return ""


def clone_urls(forge: str, gh_repo: str) -> list[str]:
    """Кандидаты для клонирования ``owner/repo``: сперва https, затем ssh.

    Порядок сохранён от #377: публичный репозиторий клонируется по https без
    единой настройки на сервере, а ssh с deploy key — запасной путь для
    приватного. Что здесь ново — ХОСТ берётся у форжа, а не вписан литералом.

    До #1118 обе строки были захардкожены на github.com, и провижининг
    GitVerse-проекта молча уезжал на чужую площадку: 01.09.2026 проект #8
    (forge=gitverse) склонировался с github и отчитался provision_status=ok.
    """
    slug = gh_repo.strip().strip("/").removesuffix(".git")
    host = git_host(forge)
    return [f"https://{host}/{slug}.git", f"git@{host}:{slug}.git"]


__all__ = [
    "GitHubForge",
    "GitVerseForge",
    "client_for",
    "clone_urls",
    "forge_of_host",
    "git_host",
]
