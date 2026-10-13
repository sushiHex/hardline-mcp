"""The mutation runner must distinguish coverage from a broken test run."""

import pytest

from scripts.mutate import _baseline, _selects, check


def test_a_selector_names_a_test_or_one_of_its_parameters():
    """The one baseline is read back per case through these matches: a near
    name or another file must not count as the case's test having run."""
    key = ("tests.test_a", "test_x[1]")
    assert _selects("tests/test_a.py::test_x", key)
    assert _selects("tests/test_a.py::test_x[1]", key)
    assert not _selects("tests/test_a.py::test_x[2]", key)
    assert not _selects("tests/test_a.py::test_x", ("tests.test_a", "test_xy"))
    assert not _selects("tests/test_b.py::test_x", key)
    assert not _selects("tests/test_a.py", key)


def test_one_case_s_broken_baseline_does_not_sink_another(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "def test_ok():\n    pass\ndef test_bad():\n    assert False\n", encoding="utf-8"
    )
    keys, errors = _baseline(tmp_path, {
        "fine": {"tests": ["tests/test_sample.py::test_ok"]},
        "broken": {"tests": ["tests/test_sample.py::test_bad"]},
    })
    assert keys == {"fine": {("tests.test_sample", "test_ok")}}
    assert "did not pass" in errors["broken"]


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
