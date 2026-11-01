"""Формат файла спецификации ``specs/<capability>/spec.md`` и ревизии (#2214).

Один файл на способность. Требование - заголовок второго уровня с id
``REQ-<число>``; сценарий - заголовок ``### Scenario`` внутри него::

    ## REQ-1
    Obligation: Хаб отказывает во входе с пустым паролем.

    ### Scenario
    - Given: зарегистрированный пользователь
    - When: он входит с пустым паролем
    - Then: хаб отвечает 401
    - Verifiable by: test
    - Test ref: tests/test_auth.py::test_empty_password_refused

Значение поля - до конца строки; строки сразу под ним с отступом и без своего
поля продолжают его (склеиваются через пробел). Любое иное содержимое внутри
требования - в том числе строка без отступа с неизвестным ключом вроде
``Note: ...`` - ошибка: молча приписанная или пропущенная строка даёт индекс, в
котором не то, что написал автор. Текст до первого требования (заголовок, пояснения) не
разбирается.

Ревизия требования считается по НОРМАЛИЗОВАННОМУ блоку (пробелы схлопнуты,
ключи отсортированы), а не по байтам файла: перенос строки или лишний пробел не
стареют требование, и правка соседа его не трогает.
"""

from __future__ import annotations

import hashlib
import json
import re

from hub.integrations.git_spec import SPEC_PATH
from hub.models import SpecRequirement, SpecScenario

REQ_ID = re.compile(r"^REQ-[0-9]+$")

_FIELD = re.compile(r"^\s*(?:[-*]\s+)?([A-Za-z][A-Za-z_ ]*?)\s*:\s*(.*?)\s*$")
_REQ_HEADING = re.compile(r"^##(?!#)\s*(.*?)\s*$")
_SCENARIO_HEADING = re.compile(r"^###(?!#)\s*(.*?)\s*$")
_OTHER_HEADING = re.compile(r"^#+(?=\s|$)")

_REQ_FIELDS = {"obligation"}
_SCENARIO_FIELDS = ("given", "when", "then", "verifiable_by", "test_ref")
_REQUIRED_SCENARIO_FIELDS = ("given", "when", "then", "verifiable_by")


class SpecParseError(ValueError):
    """Файл не разобран; текст называет строку."""


def capability_of(path: str) -> str:
    """``auth`` из ``specs/auth/spec.md``; пустая строка - путь вне формата."""
    match = SPEC_PATH.match(path)
    return match.group(1) if match else ""


def _norm(value: str) -> str:
    return " ".join(value.split())


def _field_key(raw: str) -> str:
    return re.sub(r"[\s_]+", "_", raw.strip().lower())


class _Parser:
    def __init__(self) -> None:
        self.found: list[SpecRequirement] = []
        self.req: dict[str, str] | None = None
        self.scenarios: list[SpecScenario] = []
        self.req_line = 0
        self.scenario: dict[str, str] | None = None
        self.last: tuple[dict[str, str], str] | None = None

    def feed(self, lineno: int, raw: str) -> None:
        line = raw.rstrip()
        if not line.strip():
            self.last = None
            return
        if self._heading(lineno, line):
            return
        field = _FIELD.match(line)
        if field and self._field(lineno, _field_key(field.group(1)), field.group(2)):
            return
        if self.last is not None and line[0] in " \t":
            target, key = self.last
            target[key] = _norm(f"{target[key]} {line}")
            return
        if self.req is not None:
            what = "unknown field" if field else "unexpected text"
            raise SpecParseError(f"line {lineno}: {what} {line.strip()[:60]!r}")

    def _heading(self, lineno: int, line: str) -> bool:
        match = _REQ_HEADING.match(line)
        if match:
            self._open_requirement(lineno, match.group(1))
            return True
        match = _SCENARIO_HEADING.match(line)
        if match:
            self._open_scenario(lineno, match.group(1))
            return True
        if _OTHER_HEADING.match(line):
            if self.req is not None:
                raise SpecParseError(f"line {lineno}: unsupported heading {line!r}")
            return True
        return False

    def _open_requirement(self, lineno: int, req_id: str) -> None:
        self.close()
        if not REQ_ID.match(req_id):
            raise SpecParseError(
                f"line {lineno}: requirement id {req_id!r} does not match ^REQ-[0-9]+$"
            )
        self.req = {"id": req_id}
        self.scenarios = []
        self.req_line = lineno

    def _open_scenario(self, lineno: int, title: str) -> None:
        if self.req is None:
            raise SpecParseError(f"line {lineno}: scenario outside a requirement")
        if title.split(maxsplit=1)[:1] != ["Scenario"]:
            raise SpecParseError(f"line {lineno}: expected '### Scenario'")
        self._seal_scenario()
        self.scenario = {}
        self.last = None

    def _field(self, lineno: int, key: str, value: str) -> bool:
        """True когда строка - поле; False - это текст (продолжение или ошибка).

        Неизвестный ключ не поле: ``feed`` примет такую строку только как
        продолжение с отступом, иначе откажет. Молча приписать ``Note: ...``
        к предыдущему значению значило бы исказить то, что написал автор.
        """
        if key in _REQ_FIELDS:
            target = self.req
            if target is None or self.scenario is not None:
                raise SpecParseError(
                    f"line {lineno}: {key} belongs directly under a '## REQ-N' heading"
                )
        elif key in _SCENARIO_FIELDS:
            target = self.scenario
            if target is None:
                raise SpecParseError(f"line {lineno}: {key} outside a scenario")
        else:
            return False
        if key in target:
            raise SpecParseError(f"line {lineno}: {key} is given twice")
        target[key] = _norm(value)
        self.last = (target, key)
        return True

    def _seal_scenario(self) -> None:
        scenario, self.scenario = self.scenario, None
        if scenario is None or self.req is None:
            return
        missing = [k for k in _REQUIRED_SCENARIO_FIELDS if not scenario.get(k)]
        if scenario.get("verifiable_by") == "test" and not scenario.get("test_ref"):
            missing.append("test_ref")
        if missing:
            raise SpecParseError(
                f"{self.req['id']}: scenario lacks {', '.join(missing)}"
            )
        self.scenarios.append(SpecScenario(**scenario))

    def close(self) -> None:
        self._seal_scenario()
        req, self.req, self.last = self.req, None, None
        if req is None:
            return
        if not req.get("obligation"):
            raise SpecParseError(f"line {self.req_line}: {req['id']} has no obligation")
        self.found.append(
            SpecRequirement(
                id=req["id"], obligation=req["obligation"], scenarios=self.scenarios
            )
        )


def parse_spec_file(text: str) -> list[SpecRequirement]:
    """Требования файла по порядку; ``SpecParseError`` - файл не разобран.

    Дубль id внутри файла - тоже ошибка разбора: индекс держит id уникальным.
    """
    parser = _Parser()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        parser.feed(lineno, raw)
    parser.close()
    seen: set[str] = set()
    for req in parser.found:
        if req.id in seen:
            raise SpecParseError(f"{req.id} is declared twice in the file")
        seen.add(req.id)
    return parser.found


def scenarios_json(req: SpecRequirement) -> str:
    """Канонический JSON сценариев: хранится как есть и входит в хеш блока."""
    return _canonical([s.model_dump() for s in req.scenarios])


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def requirement_revision(req: SpecRequirement) -> str:
    """sha256 канонического JSON блока ``{id, obligation, scenarios}``."""
    block = {
        "id": req.id,
        "obligation": req.obligation,
        "scenarios": [s.model_dump() for s in req.scenarios],
    }
    return hashlib.sha256(_canonical(block).encode("utf-8")).hexdigest()
