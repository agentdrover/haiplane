"""Адаптеры форжей — хостингов репозиториев (#1113).

Каждый модуль здесь реализует ``protocols.ForgePlugin`` для одного форжа.
Хаб-ядро и ``git_ops`` знают только протокол.
"""

from hub.integrations.forge.github import GitHubForge
from hub.integrations.forge.gitverse import GitVerseForge
from hub.models import DEFAULT_FORGE

#: Адаптер по ИМЕНИ форжа (#1146). Единственный реестр: до него «какой
#: хостинг» было свойством ИНСТАНСА хаба — ``plugins.forge`` выбирался один раз
#: при старте и жил весь процесс, а колонку ``projects.forge`` (#1114) не читал
#: никто. Смена форжа меняла только текст в карточке.
_CLIENTS: dict[str, type] = {"github": GitHubForge, "gitverse": GitVerseForge}


def client_for(forge: str):
    """Свежий адаптер объявленного форжа.

    Незнакомое имя сводится к github — то же умолчание и по той же причине,
    что у единственного читателя (#1114): неизвестного форжа не бывает, бывает
    необъявленный, а уронить доставку из-за строки в базе хуже, чем работать по
    умолчанию.
    """
    return _CLIENTS.get(forge, _CLIENTS[DEFAULT_FORGE])()


__all__ = ["GitHubForge", "GitVerseForge", "client_for"]
