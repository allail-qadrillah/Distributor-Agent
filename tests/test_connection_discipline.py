import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
CONNECTION_FACTORY = "src/app/database/connection.py"


def _python_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _calls_psycopg_connect(file: Path) -> bool:
    tree = ast.parse(file.read_text(), filename=str(file))
    psycopg_import = _has_psycopg_connect_import(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if (
                    node.func.attr == "connect"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "psycopg"
                ):
                    return True
            elif isinstance(node.func, ast.Name):
                if node.func.id == "connect" and psycopg_import:
                    return True
    return False


def _has_psycopg_connect_import(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "psycopg":
                for alias in node.names:
                    if alias.name == "connect":
                        return True
    return False


def test_only_connection_module_creates_connections() -> None:
    violations: list[str] = []
    for py_file in _python_files(SRC):
        rel = py_file.relative_to(ROOT).as_posix()
        if rel == CONNECTION_FACTORY:
            continue
        if _calls_psycopg_connect(py_file):
            violations.append(
                f"  {rel} calls psycopg.connect() directly; "
                f"use connect() or transaction() from "
                f"app.database.connection instead (they guarantee dict_row)"
            )
    assert not violations, (
        f"{len(violations)} module(s) bypass the connection factory:\n"
        + "\n".join(violations)
    )
