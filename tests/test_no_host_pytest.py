"""The hub starts pytest in exactly one place (#1650).

Importing a task branch's conftest.py, plugins or tests runs the code an agent
pushed — on the hub host, with the hub's environment. #1650 replaced the two
paths that did it for a LIST of tests (the review brief and the epic-approve
locator check) with a read of the files through git, and left one run: a
human's explicit run-ac-tests (#1646).

This guard keeps it that way. It is protection for the SHAPES it knows — a
spawn call whose arguments name pytest, directly, through a variable of the
same function or module, or through a hub function that itself spawns — and
not a proof that no code can ever execute a branch.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
from pathlib import Path

HUB = Path(__file__).resolve().parent.parent / "hub"

# Where pytest may be started, by function. Nothing else.
ALLOWED = {
    "hub/services/ac_tests.py::default_test_runner",
}

_SPAWN = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "run",
    "Popen",
    "check_output",
    "check_call",
    "call",
    "getoutput",
    "getstatusoutput",
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execvpe",
    "execl",
    "execle",
    "execlp",
    "execlpe",
    "spawnv",
    "spawnve",
    "spawnvp",
    "spawnl",
    "spawnle",
    "spawnlp",
    "posix_spawn",
    "posix_spawnp",
    "run_in_executor",
}
# A shell line is short; a prompt that merely mentions "uv run pytest" is long.
_MAX_COMMAND_LEN = 2000
# Words that put another command in command position: ``uv run pytest``,
# ``python -m pytest``, ``env FOO=1 pytest``, ``sudo pytest``...
_WRAPPERS = {"uv", "run", "python", "python3", "-m", "env", "sudo", "exec", "nohup"}
_WRAPPERS |= {"time", "poetry", "pipenv", "xargs", "command", "pdm", "hatch"}
_PYTEST_NAMES = ("pytest", "py.test")
_SEPARATORS = re.compile(r"&&|\|\||[;|&\n]")


def _command_words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment)
    except ValueError:
        words = segment.split()
    return words


def _is_pytest_command(text: str, depth: int = 0) -> bool:
    """Whether ``text`` (an argv word or a shell script) puts pytest in command position."""
    if len(text) > _MAX_COMMAND_LEN or depth > 3:
        return False
    text = text.replace("\\\n", " ")
    for segment in _SEPARATORS.split(text):
        words = _command_words(segment.strip())
        while words and ("=" in words[0] and not words[0].startswith("-")):
            words = words[1:]  # FOO=1 prefix
        while words and words[0] in _WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-") and words[0] != "-m":
                words = words[1:]
        if not words:
            continue
        head = os.path.basename(words[0])
        if head in _PYTEST_NAMES:
            return True
        if head in ("sh", "bash", "zsh") and "-c" in words:
            rest = words[words.index("-c") + 1 :]
            if rest and _is_pytest_command(rest[0], depth + 1):
                return True
        if " " in words[0] and _is_pytest_command(words[0], depth + 1):
            return True
    return False


class _Module:
    """What one hub module binds at its top level, for resolving names."""

    def __init__(self, name: str, tree: ast.Module):
        self.name = name
        self.tree = tree
        self.assigns: dict[str, list[ast.expr]] = {}
        self.imports: dict[str, tuple[str, str]] = {}  # local -> (module, name|"")
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.assigns.setdefault(target.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self.assigns.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    self.imports[local] = (target, "")
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def _assigned(body: list[ast.stmt], name: str) -> list[ast.expr]:
    """Values assigned to ``name`` anywhere in ``body`` (not inside nested defs)."""
    out: list[ast.expr] = []
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                out.append(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                if node.value is not None:
                    out.append(node.value)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            stack.extend(ast.iter_child_nodes(node))  # type: ignore[arg-type]
    return out


class _Resolver:
    def __init__(self, modules: dict[str, _Module]):
        self.modules = modules

    def literals(
        self,
        expr: ast.AST,
        module: _Module,
        fn_body: list[ast.stmt],
        seen: frozenset[tuple[str, str]] = frozenset(),
        depth: int = 0,
    ) -> list[str]:
        """Every string an expression may carry, following names across modules."""
        out: list[str] = []
        if depth > 8:
            return out
        for node in ast.walk(expr):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append(node.value)
            elif isinstance(node, ast.Name):
                out += self._name(node.id, module, fn_body, seen, depth)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                out += self._attribute(node, module, seen, depth)
        return out

    def _name(self, name, module, fn_body, seen, depth) -> list[str]:
        key = (module.name, name)
        if key in seen:
            return []
        seen = seen | {key}
        out: list[str] = []
        for value in _assigned(fn_body, name):
            out += self.literals(value, module, fn_body, seen, depth + 1)
        for value in module.assigns.get(name, []):
            out += self.literals(value, module, [], seen, depth + 1)
        if name in module.imports:
            source, original = module.imports[name]
            target = self.modules.get(source)
            if target is not None and original:
                out += self._name(original, target, [], seen, depth + 1)
        return out

    def _attribute(self, node: ast.Attribute, module, seen, depth) -> list[str]:
        local = node.value.id  # type: ignore[attr-defined]
        if local not in module.imports:
            return []
        source, original = module.imports[local]
        dotted = f"{source}.{original}" if original else source
        target = self.modules.get(dotted) or self.modules.get(source)
        if target is None:
            return []
        return self._name(node.attr, target, [], seen, depth + 1)


def _functions(tree: ast.Module):
    """``(qualname, body)`` for every function and method, then the module level."""

    def walk(body, prefix):
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                yield f"{prefix}{node.name}", node
                yield from walk(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.ClassDef):
                yield from walk(node.body, f"{prefix}{node.name}.")

    yield from walk(tree.body, "")


def _calls_outside_functions(tree: ast.Module):
    """Calls that run at import time: module level and class bodies."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _spawn_names(trees: dict[str, ast.Module]) -> set[str]:
    """The base spawn primitives plus hub functions that call one (two rounds)."""
    names = set(_SPAWN)
    for _ in range(2):
        for tree in trees.values():
            for qual, fn in _functions(tree):
                if any(
                    isinstance(n, ast.Call) and _callee(n) in names
                    for n in ast.walk(fn)
                ):
                    names.add(qual.rsplit(".", 1)[-1])
    return names


def scan(sources: dict[str, str]) -> set[str]:
    """``{"path::function"}`` of every place that starts pytest (``<module>`` = import time)."""
    trees = {path: ast.parse(text, filename=path) for path, text in sources.items()}
    modules = {
        _module_name(path): _Module(_module_name(path), tree)
        for path, tree in trees.items()
    }
    resolver = _Resolver(modules)
    spawners = _spawn_names(trees)
    offenders: set[str] = set()

    def check(call: ast.Call, module: _Module, body: list[ast.stmt]) -> bool:
        args = [*call.args, *(k.value for k in call.keywords)]
        return any(
            _is_pytest_command(lit)
            for arg in args
            for lit in resolver.literals(arg, module, body)
        )

    for path, tree in trees.items():
        module = modules[_module_name(path)]
        for qual, fn in _functions(tree):
            if any(
                isinstance(n, ast.Call)
                and _callee(n) in spawners
                and check(n, module, fn.body)
                for n in ast.walk(fn)
            ):
                offenders.add(f"{path}::{qual}")
        if any(
            _callee(c) in spawners and check(c, module, [])
            for c in _calls_outside_functions(tree)
        ):
            offenders.add(f"{path}::<module>")
    return offenders


def _hub_sources() -> dict[str, str]:
    root = HUB.parent
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8")
        for p in sorted(HUB.rglob("*.py"))
    }


def test_pytest_runs_only_in_allowed_functions():
    """#1650 AC-5: pytest is started by the allowlisted functions and no other."""
    found = scan(_hub_sources())

    assert found == ALLOWED, (
        f"unexpected pytest start in hub/: {sorted(found - ALLOWED)}; "
        f"allowed but no longer found (the guard may be blind): "
        f"{sorted(ALLOWED - found)}"
    )


def test_a_pytest_call_added_to_another_function_fails_the_guard():
    """#1650 AC-5: the guard does fail when a new start appears elsewhere."""
    sources = _hub_sources()
    sources["hub/services/review_brief.py"] += (
        "\n\nasync def sneaky(repo_path):\n"
        "    import asyncio\n"
        '    return await asyncio.create_subprocess_exec("uv", "run", "pytest", '
        '"--collect-only", cwd=repo_path)\n'
    )

    extra = scan(sources) - ALLOWED

    assert extra == {"hub/services/review_brief.py::sneaky"}


_SHAPES = {
    "exec argv": (
        "import asyncio\n"
        "async def f():\n"
        "    await asyncio.create_subprocess_exec('uv', 'run', 'pytest', 'x')\n"
    ),
    "shell line": (
        "import asyncio\n"
        "async def f():\n"
        "    await asyncio.create_subprocess_shell('cd w && uv run pytest -q')\n"
    ),
    "dash m": (
        "import subprocess, sys\n"
        "def f():\n"
        "    subprocess.run([sys.executable, '-m', 'pytest'])\n"
    ),
    "variable argv": (
        "import asyncio\n"
        "async def f(ids):\n"
        "    cmd = ['uv', 'run', 'pytest', *ids]\n"
        "    await asyncio.create_subprocess_exec(*cmd)\n"
    ),
    "f-string shell": (
        "import os\ndef f(x):\n    os.system(f'python -m pytest {x}')\n"
    ),
    "module constant": (
        "import subprocess\n"
        "CMD = 'uv run pytest -q'\n"
        "def f():\n"
        "    subprocess.run(CMD, shell=True)\n"
    ),
    "wrapper": (
        "import subprocess\n"
        "def _sh(*a):\n"
        "    return subprocess.run(a)\n"
        "def f():\n"
        "    return _sh('uv', 'run', 'pytest')\n"
    ),
    "method": (
        "import subprocess\n"
        "class K:\n"
        "    def go(self):\n"
        "        subprocess.Popen(['pytest', '-q'])\n"
    ),
}


_ROUND2 = {
    "imported constant": {
        "hub/c.py": "PYTEST = 'pytest'\n",
        "hub/m.py": (
            "import subprocess\nfrom hub.c import PYTEST\n"
            "def f():\n    subprocess.run([PYTEST, '-q'])\n"
        ),
    },
    "module attribute": {
        "hub/cfg.py": "CMD = 'uv run pytest -q'\n",
        "hub/m.py": (
            "import subprocess\nfrom hub import cfg\n"
            "def f():\n    subprocess.run(cfg.CMD, shell=True)\n"
        ),
    },
    "aliased module": {
        "hub/cfg.py": "CMD = 'uv run pytest -q'\n",
        "hub/m.py": (
            "import subprocess\nimport hub.cfg as conf\n"
            "def f():\n    subprocess.run(conf.CMD, shell=True)\n"
        ),
    },
    "constant chain": {
        "hub/m.py": (
            "import subprocess\nPYTEST = 'pytest'\nCMD = ['uv', 'run', PYTEST]\n"
            "def f():\n    subprocess.run(CMD)\n"
        ),
    },
    "module level call": {
        "hub/m.py": "import subprocess\nsubprocess.run(['uv', 'run', 'pytest'])\n"
    },
    "class body call": {
        "hub/m.py": (
            "import subprocess\nclass K:\n    out = subprocess.run(['pytest'])\n"
        )
    },
    "quoted in sh -c": {
        "hub/m.py": (
            "import subprocess\n"
            "def f():\n    subprocess.run(['sh', '-c', \"'pytest' -q\"])\n"
        )
    },
    "multiline shell": {
        "hub/m.py": (
            "import os\n"
            "def f():\n    os.system('set -e\\ncd w\\nuv run pytest -q\\n')\n"
        )
    },
    "line continuation": {
        "hub/m.py": (
            "import os\ndef f():\n    os.system('uv run \\\\\\n  pytest -q')\n"
        )
    },
    "env prefix and chain": {
        "hub/m.py": (
            "import os\n"
            "def f():\n    os.system('FOO=1 make lint && FOO=2 python -m pytest')\n"
        )
    },
}


def test_the_guard_follows_constants_imports_and_module_level_code():
    for name, sources in _ROUND2.items():
        assert scan(sources), f"the guard missed: {name}"


def test_the_guard_recognises_every_shape_it_claims_to_cover():
    for name, source in _SHAPES.items():
        assert scan({"m.py": source}), f"the guard missed: {name}"


def test_the_guard_does_not_flag_what_is_not_a_pytest_start():
    quiet = (
        "import subprocess\n"
        "def git():\n"
        "    subprocess.run(['git', 'status'])\n"
        "def doc():\n"
        '    """Runs `uv run pytest` for you."""\n'
        "    return 'pytest is the runner name'\n"
        "def prompt(p):\n"
        "    msg = 'Проверь локально: uv run pytest tests/ -x -q, потом отпиши'\n"
        "    subprocess.run(['agent', msg])\n"
        "def path():\n"
        "    subprocess.run(['ls', '/tmp/pytest-of-user/pytest-3'])\n"
    )
    assert scan({"m.py": quiet}) == set()
