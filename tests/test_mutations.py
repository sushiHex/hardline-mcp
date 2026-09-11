"""The mutation runner must distinguish coverage from a broken test run."""

import pytest

from scripts.mutate import check


@pytest.mark.parametrize(
    "body,after,expected",
    [
        ("assert value == 1", "value = 2", None),
        ("assert value > 0", "value = 2", "not caught"),
        ("assert value == 1", "value = missing", "not caught"),
        ("pytest.skip('optional')", "value = 2", "baseline"),
        ("assert False", "value = 2", "baseline"),
    ],
)
def test_mutation_verdict_requires_real_regression(tmp_path, body, after, expected):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        f"import pytest\nvalue = 1\ndef test_value():\n    {body}\n", encoding="utf-8"
    )
    case = dict(
        file="tests/test_sample.py",
        before="value = 1",
        after=after,
        tests=["tests/test_sample.py::test_value"],
    )
    if expected:
        with pytest.raises(RuntimeError, match=expected):
            check(tmp_path, case)
    else:
        check(tmp_path, case)


def test_stale_anchor_is_an_error(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("value = 1", encoding="utf-8")
    with pytest.raises(ValueError, match="anchor"):
        check(
            tmp_path,
            dict(file="tests/test_sample.py", before="missing", after="", tests=[]),
        )


def test_empty_selection_is_an_error(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("value = 1", encoding="utf-8")
    with pytest.raises(RuntimeError, match="baseline"):
        check(
            tmp_path,
            dict(
                file="tests/test_sample.py",
                before="value = 1",
                after="value = 2",
                tests=["tests/test_sample.py"],
            ),
        )
