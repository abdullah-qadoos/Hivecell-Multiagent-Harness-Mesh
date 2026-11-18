import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent / "services" / "api"))
from todo import TodoStore  # noqa: E402


def test_flow():
    s = TodoStore()
    a, b = s.add("a"), s.add("b")
    assert a != b
    s.complete(a)
    assert [t["title"] for t in s.list(done=True)] == ["a"]
    assert [t["title"] for t in s.list(done=False)] == ["b"]
    assert s.list()[0] == {"id": a, "title": "a", "done": True}
    s.delete(b)
    assert len(s.list()) == 1


def test_errors():
    s = TodoStore()
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            s.add(bad)
    with pytest.raises(KeyError):
        s.complete(99)
    with pytest.raises(KeyError):
        s.delete(99)
