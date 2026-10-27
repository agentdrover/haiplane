# Repository bootstrap (#532).
#
# `setup` exists because the branch policy was enforced nowhere: .githooks
# carries it, and git runs it only when core.hooksPath points there. That is
# per-clone configuration, so every clone nobody configured was unprotected —
# including the operator's own, checked on 2026-08-01.
#
# The hook is armed as part of the install a person already has to run, not as
# a step they must remember afterwards. A step you must remember is the same
# failure as the hook nobody activated.

REPO ?= .

.PHONY: setup hooks doctor test test-changed gui-test lint format types budget layers migration-steps security check check-fast

# The first command in a fresh clone. Idempotent: safe to re-run.
setup:
	uv venv
	uv pip install -e .
	$(MAKE) hooks
	@echo
	$(MAKE) doctor

# Arm the pre-push hook in REPO (default: this clone). Split out so the
# bootstrap step itself can be exercised by a test rather than trusted.
# #2000: the same step records that THIS clone agrees to run `make check-fast`
# before a push (haiplane.prePushCheck). Only a person or agent arming their own
# clone sets it; the hub never writes it into the clones it prepares on its host.
hooks:
	uv run hp-git-policy activate $(REPO)
	git -C $(REPO) config haiplane.prePushCheck "make check-fast"

# Is a push from this clone actually checked? Non-zero when it is not.
doctor:
	uv run hp-git-policy doctor $(REPO)

# -n auto: прогон идёт в процессах по числу ядер. Замерено на 2771 тесте:
# 3:55 последовательно против 0:56 на восьми ядрах, состав результата тот же.
# Флаг стоит ЗДЕСЬ и в CI, а не в addopts, потому что addopts достался бы и
# репортеру AC (scripts/ci_report_to_hub.py): он читает `-v` вывод построчно
# и ждёт nodeid первым токеном, а xdist ставит перед ним `[gw0]` — каждый AC
# молча стал бы not_found. Проверено исполнением: run_nodeids с раннером
# `uv run pytest` отдаёт {nodeid: True}, с `uv run pytest -n auto` — пустой
# словарь, то есть not_found на каждом AC при зелёном CI.
test:
	uv run pytest -q -n auto

# #2000: тесты, которые стоит прогнать для этого диффа (свои, сторожа дерева,
# читающие изменённые не-py файлы). Не заменяет `test`: полный набор — это CI.
# Сводка по замерам и причины выбора — в scripts/changed_tests.py.
test-changed:
	uv run python scripts/changed_tests.py

# #1996: браузерные сценарии карточки задачи (Playwright, настоящий хаб в режиме
# CSRF require). Вне `test` и `check`: им нужен Chromium (CI ставит его в job
# GUI; локально `uv run playwright install chromium` или, чтобы ничего не
# скачивать, HAIPLANE_GUI_CHANNEL=chrome). Путь tests/gui назван явно — обычный
# прогон его пропускает (addopts --ignore в pyproject.toml). Последовательно:
# сценарии делят один стенд.
gui-test:
	uv run pytest -q tests/gui

# Зелёный lint при красном шаге CI «Check formatting» стоил трёх пересдач за
# сутки (#1080): форматирование не входило в эту цель, поэтому неотформатиро-
# ванный файл проходил `make lint` и падал только в CI. `format` остаётся
# отдельной целью для тех, кому нужна только она.
lint:
	uv run ruff check hub tests
	uv run ruff format --check hub tests

format:
	uv run ruff format --check hub tests

types:
	uv run mypy hub

budget:
	uv run python scripts/complexity_budget.py
	uv run python scripts/file_size_budget.py
	uv run python scripts/mcp_catalog_budget.py
	uv run python scripts/wallclock_asserts.py

# Направления импорта между слоями hub (#1852): ledger — docs/agent-context/layer-rules.json.
layers:
	uv run python scripts/layer_rules.py

# Влитые файлы hub/migrations/steps/ не правятся (#1854); база - origin/develop.
migration-steps:
	uv run python scripts/migration_steps_add_only.py

security:
	uv run bandit -r hub -q
	uv run python scripts/secret_scan.py

# Состав обязан покрывать статические шаги CI-джоба test: сверку держит
# tests/test_local_checks_match_ci.py — новый шаг в CI без цели здесь красит
# тесты, а не проходит незамеченным (#1080).
check: lint types budget layers migration-steps security test

# #2000: то же, что `check`, но вместо полного набора — целевые тесты. Статическая
# часть обязана совпадать с `check` (держит tests/test_local_checks_match_ci.py).
# Перед пушем запускается хуком, если клон согласился (haiplane.prePushCheck).
check-fast: lint types budget layers migration-steps security test-changed
