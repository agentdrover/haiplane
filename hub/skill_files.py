"""Навыки хаба из файлов skills/<name>/SKILL.md (#1701).

Один источник текста навыка — файл в репозитории: правка проходит ревью кода и
CI, а потерянную базу можно засеять заново. Файл читается целиком, как лежит
(без правки переводов строк и front-matter): так строка базы и файл сравнимы
побайтно, и «тот же текст» — это равенство хешей, а не номеров версий.

Три навыка засеваются из кода (``hub.db``): у них два источника недопустимы,
поэтому их имена сюда не попадают, а файл с таким именем — ошибка проверки.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

#: Навыки, текст которых живёт в ``hub.db``. Файл skills/<имя> для них —
#: второй источник, и проверка его не пропускает.
CODE_SEEDED_SKILLS = frozenset(
    {"multi-agent-review", "machine-review-cycle", "executor-pair-discipline"}
)

#: Файлы, выгруженные с прода как есть, без front-matter. Добавить в них
#: front-matter значит изменить активный навык базы (новый хеш → черновик),
#: поэтому первый выкат оставляет их байт в байт. Список только сокращается:
#: правка текста — задача второго ревью.
LEGACY_NO_FRONT_MATTER = frozenset({"local-executor-hub-repo"})

#: Метки навыка при первой вставке в пустую базу; у остальных — ``[]``.
_TAGS = {
    "local-executor-hub-repo": '["executor", "workflow", "discipline", "local", "staged"]',
}

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class SkillFile:
    name: str
    kind: str
    content: str
    tags: str


def text_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def skills_root() -> Path | None:
    """Каталог навыков: в пакете (wheel) или в корне checkout, не от cwd."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "_skills", here.parent / "skills"):
        if candidate.is_dir():
            return candidate
    return None


def _front_matter(text: str) -> dict[str, object] | None:
    """Разобранный front-matter; None — блока нет или это не YAML-словарь."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        return None
    try:
        parsed = yaml.safe_load(match.group(1))
    except Exception:  # noqa: BLE001 - YAML бросает и ValueError (дата, !!int)
        return None
    return parsed if isinstance(parsed, dict) else None


def skill_file_problems(root: Path) -> list[str]:
    """Что не так с файлами под ``root``; пустой список — всё в порядке."""
    problems: list[str] = []
    for path in sorted(root.glob("*/SKILL.md")):
        directory = path.parent.name
        label = f"{path.parent.parent.name}/{directory}/SKILL.md"
        if directory in CODE_SEEDED_SKILLS:
            problems.append(f"{label}: навык засевается из кода, второй источник")
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{label}: не UTF-8")
            continue
        if directory in LEGACY_NO_FRONT_MATTER:
            continue
        fields = _front_matter(text)
        if fields is None:
            problems.append(f"{label}: front-matter отсутствует или не разбирается")
            continue
        if fields.get("name") != directory:
            problems.append(f"{label}: name не совпадает с каталогом {directory!r}")
        description = fields.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"{label}: description не непустая строка")
    return problems


def load_skill_files(root: Path | None = None) -> list[SkillFile]:
    """Годные файлы навыков. Негодный пропускается с предупреждением: старт
    хаба не должен падать из-за файла, это дело проверки CI."""
    root = root if root is not None else skills_root()
    if root is None:
        logger.warning("skills directory not found, file seeding skipped")
        return []
    problems = skill_file_problems(root)
    bad = {p.split("/")[1] for p in problems}
    for problem in problems:
        logger.warning("skill file skipped: %s", problem)
    loaded = []
    for path in sorted(root.glob("*/SKILL.md")):
        name = path.parent.name
        if name in bad:
            continue
        loaded.append(
            SkillFile(
                name=name,
                kind="skill",
                content=path.read_bytes().decode("utf-8"),
                tags=_TAGS.get(name, "[]"),
            )
        )
    return loaded
