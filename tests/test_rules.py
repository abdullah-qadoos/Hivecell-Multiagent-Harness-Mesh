import pytest
from pydantic import ValidationError

from conftest import rules
from hivecell.core.tools import Rejected, ToolRouter


def test_globs():
    r = rules(owns=["services/api/**"], can_read=["contracts/*.json"])
    assert r.may_write("services/api/app.py")
    assert r.may_write("services/api/deep/x/y.py")
    assert not r.may_write("services/apix/app.py")
    assert not r.may_write("tests/test_app.py")
    assert r.may_read("contracts/todo.json")
    assert not r.may_read("contracts/nested/todo.json")


def test_write_outside_owned_is_rejected(tmp_path):
    router = ToolRouter(rules(), tmp_path)
    router.call("write_file", {"path": "src/ok.py", "content": "x"})
    assert (tmp_path / "src/ok.py").read_text() == "x"
    with pytest.raises(Rejected):
        router.call("write_file", {"path": "tests/evil.py", "content": "x"})
    assert not (tmp_path / "tests").exists()


@pytest.mark.parametrize("path", ["../escape.py", "src/../../escape.py", "/tmp/abs.py", "C:/abs.py"])
def test_paths_cannot_escape_workspace(tmp_path, path):
    router = ToolRouter(rules(owns=["**"]), tmp_path / "ws")
    (tmp_path / "ws").mkdir()
    with pytest.raises(Rejected):
        router.call("write_file", {"path": path, "content": "x"})


def test_read_needs_permission(tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_text("a")
    router = ToolRouter(rules(), tmp_path)
    assert router.call("read_file", {"path": "docs/a.md"}) == "a"
    with pytest.raises(Rejected):
        router.call("read_file", {"path": "secret.txt"})
    assert router.call("list_files", {}) == "docs/a.md"


def test_tool_not_in_rules_is_rejected(tmp_path):
    router = ToolRouter(rules(tools=["read_file"]), tmp_path)
    with pytest.raises(Rejected):
        router.call("write_file", {"path": "src/a.py", "content": "x"})
    with pytest.raises(Rejected):
        router.call("__init__", {})


def test_unknown_tool_in_yaml_fails_validation():
    with pytest.raises(ValidationError):
        rules(tools=["read_file", "rm_rf"])
