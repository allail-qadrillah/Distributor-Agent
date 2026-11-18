from pathlib import Path

ROOT = Path(__file__).parents[1]
CHECK_DIRS = ["src", "tests"]


def _python_files() -> list[Path]:
    result: list[Path] = []
    for d in CHECK_DIRS:
        result.extend(sorted((ROOT / d).rglob("*.py")))
    return result


def test_no_crlf_in_python_source() -> None:
    crlf_files: list[str] = []
    for py_file in _python_files():
        content = py_file.read_bytes()
        if b"\r\n" in content:
            crlf_files.append(py_file.relative_to(ROOT).as_posix())
    assert not crlf_files, (
        f"{len(crlf_files)} Python file(s) have CRLF line endings "
        f"(expected LF):\n"
        + "\n".join(f"  {f}" for f in crlf_files)
        + "\n\nConvert with: dos2unix <file> or sed -i 's/\\r$//' <file>"
    )
