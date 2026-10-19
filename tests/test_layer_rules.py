"""Правила слоёв хаба (#1852, архитектура как код, часть А).

Проверка читает таблицу слоёв из docs/agent-context/layer-rules.json, строит
рёбра «модуль → модуль» обходом AST и красит CI на запрещённом направлении,
которого нет в ledger. Тест гоняет настоящую проверку по подложенным деревьям
во временном каталоге; настоящее дерево проверяет AC-3.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import layer_rules as rules  # noqa: E402

CONFIG = REPO_ROOT / "docs" / "agent-context" / "layer-rules.json"

# Представитель каждого слоя: путь файла в подложенном дереве hub/.
REPRESENTATIVES = {
    "entry": "app.py",
    "security": "auth.py",
    "services": "services/lifecycle.py",
    "repository": "repository.py",
    "db": "db.py",
    "integrations": "integrations/github.py",
    "models": "models.py",
    "poller": "poller.py",
    "support": "config.py",
}


def _module(rel: str) -> str:
    parts = ["hub", *Path(rel).with_suffix("").parts]
    return ".".join(p for p in parts if p != "__init__")


def _grow(tree: Path, files: dict[str, str]) -> Path:
    """Подложить hub/ со всеми представителями слоёв и заданными файлами."""
    pkg = tree / "hub"
    base = {rel: "" for rel in REPRESENTATIVES.values()}
    base["__init__.py"] = ""
    base["services/__init__.py"] = ""
    base["integrations/__init__.py"] = ""
    base.update(files)
    for rel, text in base.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return pkg


def _config(tmp: Path, ledger: list[dict] | None = None) -> Path:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw["ledger"] = ledger or []
    path = tmp / "layer-rules.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _run(pkg: Path, config: Path, capsys, *extra: str) -> tuple[int, str]:
    rc = rules.main(["--root", str(pkg), "--config", str(config), *extra])
    return rc, capsys.readouterr().out


def _forbidden_pairs() -> list[tuple[str, str]]:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    return [(src, dst) for src, dsts in raw["forbidden"].items() for dst in dsts]


def _statements(target: str, src_rel: str) -> dict[str, str]:
    """Один и тот же импорт цели во всех видах, какие бывают в коде."""
    mod = _module(target)
    parent, _, leaf = mod.rpartition(".")
    src = _module(src_rel).split(".")
    here = src if src_rel.endswith("__init__.py") else src[:-1]
    tgt = mod.split(".")
    common = 0
    while common < min(len(here), len(tgt) - 1) and here[common] == tgt[common]:
        common += 1
    dots = "." * (len(here) - common + 1)
    return {
        "absolute": f"import {mod}\n",
        "module via package": f"from {parent} import {leaf}\n",
        "symbol": f"from {mod} import Thing\n",
        "inside function": f"def f():\n    import {mod}\n",
        "type checking": f"if TYPE_CHECKING:\n    from {mod} import Thing\n",
        "relative": f"from {dots}{'.'.join(tgt[common:])} import Thing\n",
        "relative module": f"from {dots}{'.'.join(tgt[common:-1])} import {leaf}\n",
    }


def test_every_forbidden_direction_turns_ci_red(tmp_path, capsys):
    """AC-1: запрещённое — rc 1 с названием ребра, разрешённое — rc 0, повтор — одна запись."""
    pairs = _forbidden_pairs()
    assert len(pairs) >= 10, "the table is read"
    failures: list[str] = []
    for src_layer, dst_layer in pairs:
        src_rel, dst_rel = REPRESENTATIVES[src_layer], REPRESENTATIVES[dst_layer]
        for kind, stmt in _statements(dst_rel, src_rel).items():
            tree = tmp_path / f"{src_layer}-{dst_layer}-{kind}".replace(" ", "_")
            tree.mkdir()
            pkg = _grow(tree, {src_rel: stmt})
            rc, out = _run(pkg, _config(tree), capsys)
            edge = f"{_module(src_rel)} -> {_module(dst_rel)}"
            if rc != 1 or edge not in out:
                failures.append(f"{src_layer}→{dst_layer} [{kind}]: rc={rc} {out!r}")
    assert not failures, "\n".join(failures)


def test_the_forbidden_table_is_pinned():
    """Таблицу запретов не ослабить молча: ожидаемая копия лежит здесь, не в проверяемом файле."""
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert {k: sorted(v) for k, v in raw["forbidden"].items()} == {
        "services": ["entry"],
        "integrations": ["db", "entry", "poller", "repository", "services"],
        "repository": ["entry", "poller", "services"],
        "models": ["db", "entry", "poller", "repository", "services"],
        "db": ["entry", "poller", "services"],
        "security": ["entry"],
        "support": ["entry"],
    }


def test_a_duplicated_ledger_edge_fails(tmp_path, capsys):
    edge = {"from": "hub.models", "to": "hub.services.lifecycle"}
    pkg = _grow(tmp_path, {"models.py": "import hub.services.lifecycle\n"})
    good = dict(edge, removed_by="task:#1 — снять")
    bad = dict(edge, removed_by="TODO")
    rc, out = _run(pkg, _config(tmp_path, [bad, good]), capsys)
    assert rc == 1 and "дважды" in out, out


def test_an_import_from_an_init_module_is_an_edge_too(tmp_path, capsys):
    """AC-1: __init__ пакета — источник ребра; services/__init__ — слой services."""
    pkg = _grow(tmp_path, {"services/__init__.py": "from hub import app\n"})
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 1 and "hub.services -> hub.app" in out, out


def test_allowed_directions_stay_green(tmp_path, capsys):
    """AC-1: разрешённые направления (entry→services, services→models/repository, db→models)."""
    pkg = _grow(
        tmp_path,
        {
            "app.py": "from hub.services import lifecycle\nimport hub.models\n",
            "services/lifecycle.py": (
                "from hub import repository, models\n"
                "from hub.integrations import github\n"
                "from hub.db import get_db\n"
            ),
            "db.py": "from hub import models\nfrom hub import config\n",
            "repository.py": "from hub.models import Task\nfrom hub import db\n",
        },
    )
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 0, out


def test_a_repeated_edge_is_one_record(tmp_path, capsys):
    """AC-1: пять импортов одной пары — одна запись в отчёте."""
    body = (
        "import hub.app\nfrom hub import app\nfrom hub.app import X\n"
        "def f():\n    import hub.app\n"
        "if TYPE_CHECKING:\n    from hub.app import Y\n"
    )
    pkg = _grow(tmp_path, {"services/lifecycle.py": body})
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 1
    assert out.count("hub.services.lifecycle -> hub.app") == 1, out


def test_imports_do_not_create_implicit_parent_package_edges(tmp_path, capsys):
    """Из `import hub.services.lifecycle` не следует ребро к hub.services или hub."""
    edges = rules.collect_edges(
        _grow(tmp_path, {"app.py": "import hub.services.lifecycle\n"})
    ).edges
    assert ("hub.app", "hub.services.lifecycle") in edges
    assert ("hub.app", "hub.services") not in edges
    assert ("hub.app", "hub") not in edges


def test_unclassified_unparsable_and_bad_ledger_fail_loudly(tmp_path, capsys):
    """AC-2: пять способов покраснеть с диагностикой, «запись без ребра» и --update."""
    # 1. неотнесённый модуль
    pkg = _grow(tmp_path, {})
    (pkg / "mystery.py").write_text("", encoding="utf-8")
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 1 and "hub.mystery" in out and "без слоя" in out, out

    # 2. модуль в двух слоях
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    raw["ledger"] = []
    raw["layers"]["models"]["modules"].append("hub.mystery")
    raw["layers"]["poller"]["modules"].append("hub.mystery")
    twice = tmp_path / "twice.json"
    twice.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    rc, out = _run(pkg, twice, capsys)
    assert rc == 1 and "hub.mystery" in out and "двух слоях" in out, out
    (pkg / "mystery.py").unlink()

    # 3. неразбираемый файл
    (pkg / "broken.py").write_text("def (:\n", encoding="utf-8")
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 1 and "broken.py" in out and "не разобран" in out, out
    (pkg / "broken.py").unlink()

    # новое ребро для следующих проверок
    (pkg / "models.py").write_text("import hub.services.lifecycle\n")
    edge = {"from": "hub.models", "to": "hub.services.lifecycle"}

    # 4. некорректный removed_by (пусто, TODO, permanent без причины)
    for bad in ("", "TODO: потом", "permanent —", "task:#abc"):
        cfg = _config(tmp_path, [dict(edge, removed_by=bad)])
        rc, out = _run(pkg, cfg, capsys)
        assert rc == 1 and "removed_by" in out.lower(), (bad, out)

    # 5. плейсхолдер --update не проходит
    placeholder = rules.PLACEHOLDER
    cfg = _config(tmp_path, [dict(edge, removed_by=placeholder)])
    rc, out = _run(pkg, cfg, capsys)
    assert rc == 1 and "removed_by" in out.lower(), out

    # честная запись проходит
    ok = _config(tmp_path, [dict(edge, removed_by="task:#1 — снять")])
    rc, out = _run(pkg, ok, capsys)
    assert rc == 0, out

    # запись ledger без ребра — «удалите строку»
    (pkg / "models.py").write_text("")
    rc, out = _run(pkg, ok, capsys)
    assert rc == 1 and "удалите строку" in out and "hub.models" in out, out

    # --update: дописывает только новое, существующее не меняет
    (pkg / "models.py").write_text("import hub.services.lifecycle\n")
    (pkg / "repository.py").write_text("import hub.services.lifecycle\n")
    before = json.loads(ok.read_text(encoding="utf-8"))
    rc, _ = _run(pkg, ok, capsys, "--update")
    assert rc == 0
    after = json.loads(ok.read_text(encoding="utf-8"))
    assert after["ledger"][0] == before["ledger"][0]
    assert [(e["from"], e["to"]) for e in after["ledger"][1:]] == [
        ("hub.repository", "hub.services.lifecycle")
    ]
    assert after["ledger"][1]["removed_by"] == placeholder
    assert after["layers"] == before["layers"]
    # а следующий прогон без флагов снова красный: плейсхолдер отвергнут
    rc, out = _run(pkg, ok, capsys)
    assert rc == 1 and "hub.repository" in out, out

    # services/__init__.py — слой services
    layers = rules.load_config(CONFIG).layers
    assert rules.layers_of("hub.services", layers) == ["services"]
    assert rules.layers_of("hub.services.anything.deep", layers) == ["services"]
    assert rules.layers_of("hub.web_executor_slot", layers) == ["entry"]
    assert rules.layers_of("hub.repository_verdict", layers) == ["repository"]
    assert rules.layers_of("hub.migrations.part_3", layers) == ["db"]
    assert rules.layers_of("hub", layers) == ["support"]


def test_a_wildcard_does_not_cross_package_boundaries():
    """`hub.web_*` — модули корня, а не любой потомок с таким префиксом."""
    layers = {"entry": {"modules": ["hub.web_*"], "trees": []}}
    assert rules.layers_of("hub.web_x", layers) == ["entry"]
    assert rules.layers_of("hub.web_x.deep", layers) == []


def test_the_real_tree_matches_the_ledger(capsys):
    """AC-3: rc 0; запрещённые рёбра дерева == ключи ledger (7 пар постановки + ci_contract из #1848); модуль — ровно в одном слое."""
    config = rules.load_config(CONFIG)
    scan = rules.collect_edges(REPO_ROOT / "hub")
    assert not scan.errors, scan.errors
    assert len(scan.modules) > 150, "the whole tree is read"
    for module in scan.modules:
        assert len(rules.layers_of(module, config.layers)) == 1, module
    forbidden = {
        edge for edge in scan.edges if rules.is_forbidden(edge, config).forbidden
    }
    assert forbidden == set(config.ledger), "ledger == real forbidden edges"
    assert forbidden == {
        ("hub.db", "hub.services.lifecycle"),
        ("hub.models", "hub.services.ci_contract"),  # #1848, после замера постановки
        ("hub.models", "hub.services.executor_launch"),
        ("hub.models", "hub.services.finding_identity"),
        ("hub.models", "hub.services.project_policy"),
        ("hub.models", "hub.services.steward_corridor"),
        ("hub.repository", "hub.services.orchestration"),
        ("hub.repository", "hub.services.project_policy"),
    }
    assert rules.main([]) == 0, capsys.readouterr().out


def test_a_module_without_hub_imports_is_accepted_in_services(tmp_path, capsys):
    """#1856 кладёт hub/services/diff_scan.py без импортов из hub: правила его принимают."""
    pkg = _grow(tmp_path, {"services/diff_scan.py": "import re\nimport json\n"})
    rc, out = _run(pkg, _config(tmp_path), capsys)
    assert rc == 0, out


def test_ledger_entries_are_named_and_formatted():
    """Каждая запись настоящего ledger названа и с причиной, не плейсхолдер."""
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert len(raw["ledger"]) == 8
    for entry in raw["ledger"]:
        assert entry["removed_by"] != rules.PLACEHOLDER
        assert rules.validate_removed_by(entry["removed_by"]) is None, entry
    assert not re.search(r"TODO", CONFIG.read_text(encoding="utf-8"))
