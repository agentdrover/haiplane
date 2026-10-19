"""Корпус промахов ревью и оффлайн-счёт по нему (#1864).

Проверяется не «печатает ли скрипт числа», а правила, от которых числа
зависят: что считается обнаружением, что — ложной тревогой, что не считается
вовсе (несовпавший sha, самоотчёт, процедурная эскалация), и что план платного
прогона не запускает ничего.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

from scripts import review_eval as ev

CORPUS = Path(__file__).parent / "fixtures" / "review_eval" / "cases.json"

# Признаки секрета в метаданных корпуса: ключи провайдеров, bearer, пары KEY=.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*KEY)"
)


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def test_corpus_schema_and_size():
    corpus = ev.load_corpus(CORPUS)
    assert ev.validate_corpus(corpus) == []
    cases = corpus["cases"]
    defects = [c for c in cases if c["expectation"] == "defect"]
    cleans = [c for c in cases if c["expectation"] == "clean"]
    assert len(defects) >= 8
    assert len(cleans) >= 5
    for case in cases:
        assert re.fullmatch(r"[0-9a-f]{40}", case["submission_sha"])
        assert case["truth"]["kind"] in ("human_review", "prod_defect")
    for text in _strings(corpus):
        assert not _SECRET_RE.search(text), text
    # одна сдача — один случай: sha по (задача, поколение) не двоится
    pairs = [(c["task_id"], c["submission_generation"]) for c in cases]
    assert len(pairs) == len(set(pairs))


def test_corpus_validation_names_each_break():
    corpus = ev.load_corpus(CORPUS)
    bad = json.loads(json.dumps(corpus))
    bad["cases"][0]["submission_sha"] = "abc123"
    bad["cases"][1].pop("truth")
    bad["cases"][2]["expectation"] = "maybe"
    bad["cases"][3]["truth"] = {"kind": "human_review"}
    clean = next(c for c in bad["cases"] if c["expectation"] == "clean")
    clean.pop("delivery")
    errors = "\n".join(ev.validate_corpus(bad))
    assert "submission_sha не 40 hex" in errors
    assert "нет ссылки на истину" in errors
    assert "expectation вне" in errors
    assert "без update_id" in errors
    assert "нет записи о доставке" in errors


def test_window_provisional_by_corpus_fields_not_clock():
    clean = {"delivery": {"at": "2026-10-10T00:00"}, "checked_at": "2026-10-19"}
    assert ev.is_provisional({**clean, "window_days": 14})
    # окно меньше 14 дней не бывает: 9 и 0 не делают случай окончательным
    assert ev.is_provisional({**clean, "window_days": 9})
    assert ev.is_provisional({**clean, "window_days": 0})
    done = {"delivery": {"at": "2026-10-01T00:00"}, "checked_at": "2026-10-19"}
    assert not ev.is_provisional({**done, "window_days": 14})


def test_finding_match_window_and_file_rules():
    locs = [{"file": "hub/a.py", "start_line": 50, "end_line": 52}]
    assert ev.finding_matches({"file": "hub/a.py", "line": 70}, locs)
    assert ev.finding_matches({"file": "./hub/a.py", "line": 72}, locs)
    assert not ev.finding_matches({"file": "hub/a.py", "line": 73}, locs)
    assert not ev.finding_matches({"file": "hub/a.py", "line": 29}, locs)
    assert ev.finding_matches(
        {"file": "hub/a.py", "start_line": 10, "end_line": 31}, locs
    )
    assert not ev.finding_matches({"file": "hub/b.py", "line": 50}, locs)
    assert not ev.finding_matches({"file": "hub/a.py"}, locs)
    assert not ev.finding_matches("текст", locs)


# --- синтетическая база ------------------------------------------------------

_SHA = {n: f"{n:040x}" for n in range(1, 12)}


def _case(n, expectation, locators=None, sha=None):
    case = {
        "case_id": f"c{n}",
        "task_id": n,
        "submission_generation": 1,
        "submission_sha": sha or _SHA[n],
        "expectation": expectation,
        "truth": {
            "kind": "human_review",
            "update_id": n,
            "verdict": "CHANGES_REQUESTED" if expectation == "defect" else "APPROVED",
        },
    }
    if expectation == "defect":
        case["defect_class"] = "k"
        case["locators"] = (
            locators
            if locators is not None
            else [{"file": "src/a.py", "start_line": 50, "end_line": 50}]
        )
    else:
        case["delivery"] = {"update_id": n, "at": "2026-10-01T00:00"}
        case["checked_at"] = "2026-10-19"
        case["window_days"] = 14
    return case


async def _task(db, n):
    await db.execute("INSERT INTO tasks (id, title) VALUES (?, 't')", (n,))
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha) VALUES (?, 1, ?)",
        (n, _SHA[n]),
    )


async def _report(db, task, confirmed=(), unresolved=(), self_reviewed=0, carried=None):
    cur = await db.execute(
        "INSERT INTO machine_reviews (task_id, submission_generation, "
        "findings_confirmed, unresolved, self_reviewed, carried_from_review_id) "
        "VALUES (?, 1, ?, ?, ?, ?)",
        (
            task,
            json.dumps(list(confirmed)),
            json.dumps(list(unresolved)),
            self_reviewed,
            carried,
        ),
    )
    return cur.lastrowid


async def _judge(
    db,
    task,
    verdict,
    reason="",
    contour=2,
    kind="verdict",
    submitted=None,
    judged_id=None,
):
    cur = await db.execute(
        "INSERT INTO steward_judgements (task_id, generation, kind, "
        "submitted_verdict, verdict, escalate_reason, contour, judged_id) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (task, kind, submitted or verdict, verdict, reason, contour, judged_id),
    )
    return cur.lastrowid


def _finding(file, line):
    return {"title": "f", "severity": "high", "file": file, "line": line}


@pytest.fixture
async def synthetic(db, tmp_path):
    """Шесть сдач: дефект во втором отчёте, процедурный escalate, находка вне
    окна, ложная тревога при concur, чужой sha, процедурный object."""
    for n in range(1, 8):
        await _task(db, n)
    # c1: первый отчёт пуст, второй попал в окно; перенос не удваивает
    await _report(db, 1)
    second = await _report(db, 1, [_finding("src/a.py", 60)])
    await _report(db, 1, [_finding("src/a.py", 60)], carried=second)
    # c2: нераспознанная находка — тревога, не обнаружение; судья эскалировал процедурно
    await _report(db, 2, unresolved=[{"title": "u", "why": "w"}])
    await _judge(db, 2, "escalate", "no_grounds")
    # c3: другая находка в том же файле вне окна; самоотчёт с попаданием не в счёт
    await _report(db, 3, [_finding("src/a.py", 200)])
    await _report(db, 3, [_finding("src/a.py", 50)], self_reviewed=1)
    # c4: чистая с ложной находкой; судья approve, советник concur — не тревога
    await _report(db, 4, [_finding("src/z.py", 1)])
    judge = await _judge(db, 4, "approve")
    await _judge(db, 4, "concur", kind="advisor", judged_id=judge)
    # c5: sha случая не совпадает с сдачей — обнаружение не считается
    await _report(db, 5, [_finding("src/a.py", 50)])
    # c6: судья approve, советник object по существу — поймал
    await _report(db, 6)
    judge = await _judge(db, 6, "approve")
    await _judge(db, 6, "object", kind="advisor", judged_id=judge)
    # c7: object, превращённый из concur процедурно, — отдельная строка
    await _report(db, 7)
    judge = await _judge(db, 7, "approve")
    await _judge(
        db,
        7,
        "object",
        "low_confidence",
        kind="advisor",
        submitted="concur",
        judged_id=judge,
    )
    await db.commit()
    corpus = {
        "version": 1,
        "defect_classes": ["k"],
        "cases": [
            _case(1, "defect"),
            _case(2, "defect"),
            _case(3, "defect"),
            _case(4, "clean"),
            _case(5, "defect", sha="f" * 40),
            _case(6, "defect"),
            _case(7, "defect"),
        ],
    }
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    return path


def _rows(corpus_path, db_dsn):
    corpus = ev.load_corpus(corpus_path)
    conn = ev.open_readonly(db_dsn)
    try:
        return {
            r["case_id"]: r
            for r in (ev.evaluate_case(conn, c) for c in corpus["cases"])
        }
    finally:
        conn.close()


async def test_offline_score_recall_and_false_alarms(
    synthetic, db_dsn, capsys, monkeypatch
):
    rows = _rows(synthetic, db_dsn)
    assert rows["c1"]["machine"] == "detected"  # второй отчёт поколения
    assert rows["c2"]["machine"] == "alerted"
    assert rows["c2"]["judge"] == "procedural"
    assert rows["c3"]["machine"] == "missed"  # вне окна; самоотчёт не в счёт
    assert rows["c4"]["machine"] == "false_alarm"
    assert rows["c4"]["advisor"] == "ok"  # concur на чистой — не тревога
    assert rows["c5"]["status"] == "sha_mismatch"
    assert "machine" not in rows["c5"]
    assert rows["c6"]["advisor"] == "caught"
    assert rows["c7"]["advisor"] == "procedural_object"

    def no_network(*a, **k):
        raise AssertionError("сеть недоступна оффлайн-счёту")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    argv = ["--corpus", str(synthetic), "--db", db_dsn]
    assert ev.main(argv) == 0
    first = capsys.readouterr().out
    assert ev.main(argv) == 0
    assert capsys.readouterr().out == first
    assert "sha_mismatch 1" in first
    assert (
        "полнота на дефектных: 1/5 (20.0%)" in first
    )  # c1; c2 насторожился, c3, c6, c7 — нет
    assert "ложные тревоги на чистых: 1/1 (100.0%)" in first
    assert "процедурная эскалация 1" in first
    assert "procedural_object 1" in first


async def test_score_does_not_write_the_database(synthetic, db_dsn):
    before = Path(db_dsn).read_bytes()
    _rows(synthetic, db_dsn)
    assert Path(db_dsn).read_bytes() == before
    conn = ev.open_readonly(db_dsn)
    try:
        with pytest.raises(Exception, match="readonly"):
            conn.execute("DELETE FROM machine_reviews")
    finally:
        conn.close()


async def test_missing_data_is_not_zero(db, db_dsn, tmp_path):
    await _task(db, 1)
    await db.commit()
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {"version": 1, "defect_classes": ["k"], "cases": [_case(1, "defect")]}
        ),
        encoding="utf-8",
    )
    row = _rows(path, db_dsn)["c1"]
    assert row["machine"] == "no_report"
    assert row["judge"] == "none"
    assert row["advisor"] == "not_applicable"
    assert ev.main(["--corpus", str(path), "--db", db_dsn]) == 0


async def test_plan_cap_prints_plan_only(db, db_dsn, tmp_path, capsys):
    for n in range(1, 6):
        await _task(db, n)
    rows = [
        (1, "bc-1", 300, 100),  # выгрузка важнее API
        (3, "bc-3", None, 70),  # только API — нижняя граница
        (4, "bc-4", None, None),  # нет данных — не ноль
        (5, "bc-5", 500, None),
    ]
    for task, agent, billed, provider in rows:
        await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, billed_tokens, provider_tokens) VALUES (?, 1, ?, ?, ?)",
            (task, agent, billed, provider),
        )
    await db.commit()
    # два дефекта одной сдачи: одна цель после дедупликации
    twin = {**_case(1, "defect"), "case_id": "c1b"}
    cases = [
        _case(1, "defect"),
        twin,
        _case(3, "defect"),
        _case(4, "defect"),
        _case(5, "clean"),
    ]
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"version": 1, "defect_classes": ["k"], "cases": cases}),
        encoding="utf-8",
    )
    assert ev.main(["--corpus", str(path), "--db", db_dsn, "--plan-cap", "3"]) == 0
    out = capsys.readouterr().out
    plan = out.split("ПЛАН ПЕРЕПРОГОНА")[1]
    assert "целей без дублей: 4, в плане по потолку 3: 3" in plan
    assert plan.count(f"#1 gen1 {_SHA[1][:12]}") == 1  # без дублей
    assert "c1, c1b" in plan
    assert "300 токенов (выгрузка, billed_tokens)" in plan
    assert "70 токенов (нижняя граница: provider_tokens)" in plan
    assert "нет данных; случаи: c4" in plan
    assert "оценка цены: ≥ 370 токенов" in plan
    assert "целей без данных о цене: 1" in plan
    assert "не вошло в план из-за потолка: 1" in plan
    # план не пишет в базу и ничего не запускает: счётчик прогонов тот же
    conn = ev.open_readonly(db_dsn)
    try:
        assert conn.execute("SELECT COUNT(*) FROM review_dispatches").fetchone()[0] == 4
    finally:
        conn.close()


async def test_plan_marks_unknown_cost(db, db_dsn, tmp_path, capsys):
    await _task(db, 1)
    await db.commit()
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {"version": 1, "defect_classes": ["k"], "cases": [_case(1, "defect")]}
        ),
        encoding="utf-8",
    )
    assert ev.main(["--corpus", str(path), "--db", db_dsn, "--plan-cap", "5"]) == 0
    out = capsys.readouterr().out
    assert "нет данных; случаи: c1" in out
    assert "оценка цены: нет данных" in out
    assert "целей без данных о цене: 1 — в сумму не входят" in out


def test_invalid_corpus_stops_the_run(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"cases": []}), encoding="utf-8")
    assert ev.main(["--corpus", str(path), "--db", str(tmp_path / "x.db")]) == 1
    assert "корпус пуст" in capsys.readouterr().out


def test_unresolved_about_another_file_is_not_an_alert():
    locs = [{"file": "a.py", "start_line": 1, "end_line": 1}]
    case = {"expectation": "defect", "locators": locs}
    other = [{"id": 1, "confirmed": [], "unresolved": [{"title": "t", "file": "b.py"}]}]
    bare = [{"id": 1, "confirmed": [], "unresolved": [{"title": "t", "why": "w"}]}]
    assert ev.machine_outcome(case, other) == "missed"
    assert ev.machine_outcome(case, bare) == "alerted"


async def test_clean_shares_are_marked_provisional(synthetic, db_dsn, capsys):
    assert ev.main(["--corpus", str(synthetic), "--db", db_dsn]) == 0
    out = capsys.readouterr().out
    assert "предварительно: окно не истекло у 0" not in out
    assert out.count("чистых в счёте") in (0, 3)  # окно синтетической чистой истекло


def test_validator_rejects_short_window_bool_and_bad_date():
    corpus = ev.load_corpus(CORPUS)
    clean = next(c for c in corpus["cases"] if c["expectation"] == "clean")
    for field, value, needle in (
        ("window_days", 9, "меньше 14"),
        ("window_days", True, "нет checked_at и window_days"),
        ("checked_at", "вчера", "не дата"),
    ):
        bad = json.loads(json.dumps(corpus))
        target = next(c for c in bad["cases"] if c["case_id"] == clean["case_id"])
        target[field] = value
        assert needle in "\n".join(ev.validate_corpus(bad)), (field, value)
    bad = json.loads(json.dumps(corpus))
    next(c for c in bad["cases"] if c["expectation"] == "defect")["locators"][0][
        "start_line"
    ] = True
    assert "диапазон строк неверен" in "\n".join(ev.validate_corpus(bad))


async def test_plan_skips_sha_mismatch_and_judge_counts_substance_only(
    db, db_dsn, tmp_path, capsys
):
    for n in (1, 2, 3):
        await _task(db, n)
    for n in (1, 2, 3):
        await db.execute(
            "INSERT INTO review_dispatches (task_id, submission_generation, "
            "agent_id, billed_tokens) VALUES (?, 1, 'bc', ?)",
            (n, 9_000_000 if n == 1 else 100),
        )
    await _judge(db, 2, "escalate", "no_grounds")  # процедурный отказ — не в счёте
    await _judge(db, 3, "changes_requested", "")
    await db.commit()
    # c1 — чужой sha и дорогой чужой прогон; он не должен занять место под потолком
    cases = [_case(1, "defect", sha="e" * 40), _case(2, "defect"), _case(3, "defect")]
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps({"version": 1, "defect_classes": ["k"], "cases": cases}),
        encoding="utf-8",
    )
    assert ev.main(["--corpus", str(path), "--db", db_dsn, "--plan-cap", "2"]) == 0
    out = capsys.readouterr().out
    plan = out.split("ПЛАН ПЕРЕПРОГОНА")[1]
    assert "целей без дублей: 2, в плане по потолку 2: 2" in plan
    assert "исключено из плана: 1" in plan
    assert "9000000" not in plan
    assert "оценка цены: 200 токенов" in plan
    judge = out.split("СУДЬЯ СТЮАРДА")[1].split("СОВЕТНИК")[0]
    assert "удержал дефектную (не approve): 1/1 (100.0%)" in judge  # c3
    assert "0/1" not in judge
    # без единого суждения по существу цифры нет, а не 0/1
    only_procedural = ev.render_report(
        [{**row, "judge": "procedural"} for row in _FAKE_ROWS]
    )
    assert "удержал дефектную (не approve): нет суждений по существу" in only_procedural
    assert "вне знаменателя, дефектные: процедурная эскалация 1" in judge


_FAKE_ROWS = [
    {
        "case_id": "x",
        "task_id": 1,
        "generation": 1,
        "expectation": "defect",
        "provisional": False,
        "status": "scored",
        "machine": "missed",
        "advisor": "not_applicable",
    }
]
