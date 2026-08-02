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

.PHONY: setup hooks doctor test lint format check

# The first command in a fresh clone. Idempotent: safe to re-run.
setup:
	uv venv
	uv pip install -e .
	$(MAKE) hooks
	@echo
	$(MAKE) doctor

# Arm the pre-push hook in REPO (default: this clone). Split out so the
# bootstrap step itself can be exercised by a test rather than trusted.
hooks:
	uv run oc-git-policy activate $(REPO)

# Is a push from this clone actually checked? Non-zero when it is not.
doctor:
	uv run oc-git-policy doctor $(REPO)

test:
	uv run pytest -q

lint:
	uv run ruff check hub tests

format:
	uv run ruff format --check hub tests

check: lint format test
