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
#: нужны ни токен, ни сеть), #1146 — для ВЫЗОВОВ. Две карты с одинаковым
#: содержимым разошлись бы ровно там, где это дороже всего: добавили третий
#: форж в одну — и ссылки на него ведут в никуда, либо вызовы уходят в GitHub.
#: Правило «какой форж» и так живёт в единственном читателе (#1114); второй
#: копии таблицы «чем его обслуживать» здесь тоже не заводится.
_BY_NAME: dict[str, type] = {"github": GitHubForge, "gitverse": GitVerseForge}


def client_for(forge: str):
    """Свежий адаптер объявленного форжа (#1146).

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


__all__ = ["GitHubForge", "GitVerseForge", "client_for", "pr_url", "repo_url"]
