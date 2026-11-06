"""Every write names its columns (CLAUDE.md, "Never reshape a table in place").

Revisions of this code run against one store for as long as their processes
live. A positional ``INSERT INTO t VALUES (...)`` fails in all of them the moment
any revision adds a column to ``t``, so no writer may rely on a table's shape.
"""

import ast
import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "hardline_mcp"

# INSERT [OR x] / REPLACE INTO [schema.]table, then VALUES or SELECT: a table
# name with no column list. Names may be quoted ("t", `t`, [t]; escaped inside
# a Python string). Strings are adjacent literals in the source, so whitespace
# between them is optional.
_NAME = r"[\w\"`\[\]\\]+"
POSITIONAL = re.compile(
    rf"(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+(?:{_NAME}\.)?{_NAME}\s*(?:VALUES|SELECT)\b",
    re.IGNORECASE,
)


def string_values(source: str) -> list[str]:
    """Every string in ``source`` as the parser reads it: adjacent literals
    joined whatever their quotes or prefixes, and an f-string's placeholder
    standing in as a name (``f"INSERT INTO {table} VALUES"``)."""
    values = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            values.append("".join(
                part.value if isinstance(part, ast.Constant) else "table"
                for part in node.values
            ))
    return values


def positional_inserts() -> list[str]:
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        for text in string_values(path.read_text(encoding="utf-8")):
            found += [f"{path.relative_to(SOURCE)}: {m.group(0)}" for m in POSITIONAL.finditer(text)]
    return found


def test_no_insert_relies_on_a_tables_column_order():
    assert positional_inserts() == []


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (?)",
        "insert or replace into t values (?)",
        "REPLACE INTO t VALUES (?)",
        'INSERT INTO "codex_routes" VALUES (?)',
        'INSERT INTO \\"codex_routes\\" VALUES (?)',
        "INSERT INTO `t` VALUES (?)",
        "INSERT INTO [t] VALUES (?)",
        "INSERT INTO main.t VALUES (?)",
        "INSERT INTO t SELECT * FROM u",
        "INSERT INTO t\n    VALUES (?)",
    ],
)
def test_the_guard_sees_a_positional_insert(sql):
    assert POSITIONAL.search(sql), sql


@pytest.mark.parametrize(
    "code",
    [
        'q = f"INSERT INTO {table} VALUES (?, ?)"',
        'q = "INSERT INTO lane_hints" " VALUES (?, ?)"',
        "q = ('INSERT INTO lane_hints'\n' VALUES (?)')",
        'q = ("INSERT INTO t"\n rf" VALUES (?)")',
    ],
    ids=["placeholder-table", "adjacent-one-line", "single-quoted", "raw-f-prefix"],
)
def test_the_scan_sees_a_positional_insert_however_the_string_is_written(code):
    assert any(POSITIONAL.search(s) for s in string_values(code)), code


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t (a, b) VALUES (?, ?)",
        'INSERT OR REPLACE INTO "t" (a) VALUES (?)',
        "INSERT INTO main.t (a) SELECT a FROM u",
        "INSERT INTO t DEFAULT VALUES",
    ],
)
def test_the_guard_passes_named_columns(sql):
    assert not POSITIONAL.search(sql), sql
