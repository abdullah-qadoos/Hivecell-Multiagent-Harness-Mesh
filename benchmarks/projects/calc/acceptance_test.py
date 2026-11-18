import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "services" / "api"))
from calc import evaluate  # noqa: E402


@pytest.mark.parametrize("expr, want", [
    ("1 + 2 * 3", 7), ("(1+2)*3", 9), ("-4 + 10 / 4", -1.5), ("2*(3+(4-1))", 12),
    ("10 - 2 - 3", 5), ("8 / 2 / 2", 2), ("--3", 3), ("1.5 * 2", 3),
])
def test_values(expr, want):
    assert evaluate(expr) == pytest.approx(want)


@pytest.mark.parametrize("bad", ["", "2+", "abc", "1 2", "(1+2", "1+2)", "__import__('os')"])
def test_malformed(bad):
    with pytest.raises(ValueError):
        evaluate(bad)


def test_division_by_zero():
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / (2 - 2)")
