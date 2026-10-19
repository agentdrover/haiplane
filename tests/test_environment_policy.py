"""Сторож пропусков по окружению (#1851): в CI ничего не пропускается."""

from __future__ import annotations

import inspect

from tests import env_caps


def _predicates() -> dict[str, object]:
    return {
        name: fn
        for name, fn in inspect.getmembers(env_caps, inspect.isfunction)
        if not name.startswith("_") and fn.__module__ == env_caps.__name__
    }


def test_every_env_cap_runs_in_ci(monkeypatch) -> None:
    """AC-2 (#1851): без возможности и без CI — пропуск с причиной, в CI — нет."""
    predicates = _predicates()
    assert predicates, "в tests/env_caps.py нет предикатов: сторож ничего не проверяет"
    for name in env_caps.PROBES:
        monkeypatch.setitem(env_caps.PROBES, name, lambda: False)

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    for name, fn in predicates.items():
        reason = fn()
        assert isinstance(reason, str) and reason.strip(), (
            f"{name}: без CI и без возможности нужна причина пропуска"
        )

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    for name, fn in predicates.items():
        assert fn() is None, f"{name}: в CI тест обязан выполняться, а не пропускаться"


def test_every_env_probe_is_used_by_a_predicate(monkeypatch) -> None:
    """Проба без предиката — мёртвый код, который сторож не проверяет."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    predicates = _predicates()
    for probe in env_caps.PROBES:
        for name in env_caps.PROBES:
            monkeypatch.setitem(env_caps.PROBES, name, lambda: True)
        monkeypatch.setitem(env_caps.PROBES, probe, lambda: False)
        assert any(fn() for fn in predicates.values()), probe
