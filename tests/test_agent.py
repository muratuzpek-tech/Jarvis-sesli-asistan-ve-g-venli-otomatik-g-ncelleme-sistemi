"""tests/test_agent.py — ReAct regression tests"""
import sys
sys.path.insert(0, '.')
from tools.agent.react_runtime import _parse_json_response, ReActStep


def test_json_direct():
    r = _parse_json_response('{"tool": "finish_task"}')
    assert r["tool"] == "finish_task"

def test_json_markdown():
    r = _parse_json_response('```json\n{"tool": "run"}\n```')
    assert r["tool"] == "run"

def test_json_embedded():
    r = _parse_json_response('thinking... {"tool": "write"} done')
    assert r["tool"] == "write"

def test_json_fail_safe():
    r = _parse_json_response("garbage")
    assert r == {}

def test_step_structure():
    s = ReActStep(turn=1, thought="test", tool="t", args={"a": 1}, observation="ok")
    assert s.turn == 1
    assert not s.finished
