import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "services" / "api"))
from lru import LRUCache  # noqa: E402


def test_eviction_respects_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1  # a is now most recent
    c.put("c", 3)  # evicts b
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    assert len(c) == 2


def test_update_refreshes_and_default():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 10)  # update makes a most recent
    c.put("c", 3)  # evicts b
    assert c.get("a") == 10 and c.get("b", "gone") == "gone"


def test_capacity_validation():
    with pytest.raises(ValueError):
        LRUCache(0)
