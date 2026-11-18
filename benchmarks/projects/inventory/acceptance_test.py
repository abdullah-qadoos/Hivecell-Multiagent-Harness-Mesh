import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "services" / "api"))
from inventory import Inventory  # noqa: E402


def test_stock_flow():
    inv = Inventory()
    inv.add("b", 5)
    inv.add("a", 2)
    inv.add("b", 1)
    assert inv.quantity("b") == 6 and inv.quantity("zzz") == 0
    inv.remove("a", 2)
    assert inv.quantity("a") == 0
    assert inv.low_stock(3) == ["a"]
    assert inv.low_stock(10) == ["a", "b"]


def test_errors_change_nothing():
    inv = Inventory()
    inv.add("a", 1)
    with pytest.raises(ValueError):
        inv.remove("a", 2)
    assert inv.quantity("a") == 1
    with pytest.raises(KeyError):
        inv.remove("nope", 1)
    for bad in (0, -1, 1.5):
        with pytest.raises(ValueError):
            inv.add("a", bad)
