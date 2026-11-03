"""The core's import boundary, enforced rather than documented.

models.py promises that nothing in the core imports FastAPI, SQLite, a UI layer
or the PayPal SDK, and that adapters translate outside data into the core types.
These tests hold that promise to the actual import graph, so the next dependency
added has to be a deliberate decision rather than an accident.
"""

import ast
import pathlib
import sys

import pytest

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "src" / "reconcile"
MODULES = sorted(PACKAGE.glob("*.py"))

# The adapter layer: allowed to talk to the outside world, still stdlib-only here.
ADAPTERS = {"paypal.py"}

# Anything that would drag a framework, a database or a vendor SDK into the core.
FORBIDDEN_PREFIXES = (
    "fastapi", "starlette", "flask", "django", "uvicorn", "pydantic",
    "sqlite3", "sqlalchemy", "psycopg", "psycopg2", "asyncpg", "alembic", "redis",
    "paypalrestsdk", "paypalhttp", "paypalcheckoutsdk", "braintree",
    "react", "jinja2", "streamlit",
    "openai", "anthropic", "langchain", "transformers", "torch",
    "requests", "httpx", "aiohttp", "urllib3",
    "pandas", "numpy",
)


def imported_roots(path: pathlib.Path) -> set[str]:
    """Top-level names this module imports absolutely (relative imports excluded)."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            roots.add((node.module or "").split(".")[0])
    return roots


def test_the_package_was_actually_found():
    assert MODULES, f"no modules discovered under {PACKAGE}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_module_imports_nothing_outside_the_standard_library(path):
    external = imported_roots(path) - set(sys.stdlib_module_names)
    assert external == set(), f"{path.name} imports non-stdlib {sorted(external)}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_framework_database_or_vendor_sdk(path):
    offenders = {
        root
        for root in imported_roots(path)
        if root.lower().startswith(FORBIDDEN_PREFIXES)
    }
    assert offenders == set(), f"{path.name} imports forbidden {sorted(offenders)}"


def test_only_the_adapter_layer_speaks_http():
    """The core must not reach the network; that is the adapter's job alone."""
    speakers = {
        path.name
        for path in MODULES
        if {"urllib", "http", "socket", "ssl"} & imported_roots(path)
    }
    assert speakers <= ADAPTERS, f"non-adapter modules import transport: {sorted(speakers - ADAPTERS)}"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_module_imports_a_private_name_from_a_sibling(path):
    """F7: matchers must not reach into another module's private internals."""
    offenders = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level:
            for alias in node.names:
                if alias.name.startswith("_"):
                    offenders.append(alias.name)
    assert offenders == [], f"{path.name} imports private {offenders}"


# Composition roots: allowed to wire the adapter to the core, because that is
# their job. __init__.py exposes it lazily via __getattr__ and __main__.py
# imports it inside the --paypal branch, so importing the core pulls in neither.
COMPOSITION_ROOTS = {"__init__.py", "__main__.py"}


def test_the_core_does_not_import_the_adapter():
    """paypal.py may depend on the core; the core may not depend on paypal."""
    for path in MODULES:
        if path.name in ADAPTERS | COMPOSITION_ROOTS:
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.level:
                assert node.module != "paypal", f"{path.name} imports the adapter"
                for alias in node.names:
                    assert alias.name != "paypal", f"{path.name} imports the adapter"


def test_importing_the_core_does_not_pull_in_the_adapter():
    """Proven at runtime, not just in the import graph."""
    import subprocess

    probe = (
        "import sys; import reconcile; "
        "print('reconcile.paypal' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(PACKAGE.parents[1]),
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout


def test_the_composition_roots_import_the_adapter_lazily():
    """A top-level adapter import in a composition root would defeat the point."""
    for name in sorted(COMPOSITION_ROOTS):
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        for node in tree.body:  # module level only
            assert not (
                isinstance(node, ast.ImportFrom)
                and node.level
                and (node.module == "paypal"
                     or any(a.name == "paypal" for a in node.names))
            ), f"{name} imports the adapter at module level"
