from conftest import FakeModel, call, rules
from hivecell.core.loop import run_agent
from hivecell.core.tools import ToolRouter
from hivecell.models.anthropic_model import to_anthropic
from hivecell.models.openai_compat import _args, to_openai


def run(model, tmp_path, r=None, **kw):
    events = []
    r = r or rules()
    res = run_agent(r, model, ToolRouter(r, tmp_path), "task", lambda t, p: events.append((t, p)), **kw)
    return res, events


def test_writes_then_reports_done(tmp_path):
    model = FakeModel([[call("write_file", path="src/a.py", content="x = 1")], [call("report_done", summary="ok")]])
    res, _ = run(model, tmp_path)
    assert (res.status, res.summary, res.turns) == ("done", "ok", 2)
    assert (res.input_tokens, res.output_tokens) == (200, 200)
    assert (tmp_path / "src/a.py").exists()


def test_rejected_write_is_reported_back_to_model(tmp_path):
    model = FakeModel([[call("write_file", path="tests/x.py", content="")], [call("report_done", summary="")]])
    res, events = run(model, tmp_path)
    assert res.status == "done"
    assert any(t == "rejected" for t, _ in events)
    tool_msgs = [m for m in model.last_messages if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("REJECTED")
    assert not (tmp_path / "tests").exists()


def test_bad_args_become_an_error_not_a_crash(tmp_path):
    model = FakeModel([[call("read_file", nope=1)], [call("report_done", summary="")]])
    res, events = run(model, tmp_path)
    assert res.status == "done"
    assert any(t == "tool_error" for t, _ in events)


def test_token_budget_stops_agent(tmp_path):
    model = FakeModel([[call("list_files")]] * 50, tokens_per_turn=1000)
    res, _ = run(model, tmp_path, token_limit=5000)
    assert res.status == "over_tokens"
    assert res.turns == 3  # 2000 tokens per turn, stops once past 5000


def test_max_turns(tmp_path):
    res, _ = run(FakeModel([]), tmp_path, max_turns=3)
    assert res.status == "max_turns"


def test_adapters_convert_a_tool_round_trip():
    c1, c2 = call("read_file", path="a"), call("list_files")
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "text": "", "tool_calls": [c1, c2]},
        {"role": "tool", "id": c1.id, "content": "A"},
        {"role": "tool", "id": c2.id, "content": "B"},
    ]
    a = to_anthropic(msgs)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in a[2]["content"]] == [c1.id, c2.id]
    o = to_openai(msgs)
    assert [m["role"] for m in o] == ["user", "assistant", "tool", "tool"]
    assert o[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert _args("not json") == {"_invalid_arguments": "not json"}
