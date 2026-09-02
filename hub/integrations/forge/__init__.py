"""Адаптеры форжей — хостингов репозиториев (#1113).

Каждый модуль здесь реализует ``protocols.ForgePlugin`` для одного форжа.
Хаб-ядро и ``git_ops`` знают только протокол.
"""

from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge
from hub.models import DEFAULT_FORGE

#: Адаптер по ИМЕНИ форжа. ОДИН реестр на все нужды, и это не косметика.
#:
#: #1119 завела здесь карту для АДРЕСОВ (ссылка — чистое форматирование, ей не
#: нужны ни токен, ни сеть), #1118 — для провижининга, которое обязано спросить
#: ИМЕННО тот форж, что объявлен у проекта. Две карты с одинаковым содержимым
#: разошлись бы ровно там, где это дороже всего: добавили третий форж в одну —
#: и ссылки на него ведут в никуда, либо провижининг клонирует не оттуда.
#: Правило «какой форж» и так живёт в единственном читателе (#1114); второй
#: копии таблицы «чем его обслуживать» здесь тоже не заводится.
_BY_NAME: dict[str, type] = {"github": GitHubForge, "gitverse": GitVerseForge}

#: Git-хост по имени форжа (#1118).
#:
#: Отдельно от адреса API: у GitVerse это разные точки входа
#: (``api.gitverse.ru`` против ``gitverse.ru``) и разные credential'ы — токен
#: для API, deploy key для git.
_GIT_HOSTS: dict[str, str] = {"github": "github.com", "gitverse": "gitverse.ru"}


def client_for(forge: str):
    """Свежий адаптер объявленного форжа.

    Незнакомое имя сводится к github — то же умолчание и по той же причине,
    что у единственного читателя (#1114): неизвестного форжа не бывает, бывает
    необъявленный, а уронить доставку из-за строки в базе хуже, чем работать по
    умолчанию.
    """
    return _BY_NAME.get(forge, _BY_NAME[DEFAULT_FORGE])()


def repo_url(forge: str, gh_repo: str) -> str:
    """Адрес репозитория в вебе. Незнакомый форж — как github, см. #1114."""
    return client_for(forge).repo_url(gh_repo)


def pr_url(forge: str, gh_repo: str, pr_number: int) -> str:
    """Адрес запроса на слияние. У GitHub /pull/, у GitVerse /pulls/."""
    return client_for(forge).pr_url(pr_number, gh_repo)


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
    "pr_url",
    "repo_url",
]
