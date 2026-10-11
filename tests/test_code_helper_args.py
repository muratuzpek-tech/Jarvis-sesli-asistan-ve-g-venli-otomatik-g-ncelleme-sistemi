"""code_helper 'args': TOOL_DECLARATIONS'ta STRING beyan edilir; işleyici
string gelirse shlex.split ile argüman listesine çevirmeli (liste de
geçerli kalır). Betik tmp_path'te, sys.executable ile yerel çalışır."""
import json
import sys

from jarvis.actions.code_helper import code_helper

_ECHO = "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"


def _run(tmp_path, args):
    script = tmp_path / "echo_args.py"
    script.write_text(_ECHO, encoding="utf-8")
    out = code_helper({"action": "run", "file_path": str(script), "args": args})
    assert out.startswith("Output:\n"), out
    return json.loads(out.split("Output:\n", 1)[1].splitlines()[0])


def test_string_args_are_split_like_a_shell(tmp_path):
    assert _run(tmp_path, "--name 'Ali Veli' -v") == ["--name", "Ali Veli", "-v"]


def test_unbalanced_quote_falls_back_to_whitespace_split(tmp_path):
    assert _run(tmp_path, "a 'b c") == ["a", "'b", "c"]


def test_list_args_still_work(tmp_path):
    assert _run(tmp_path, ["a b", "c"]) == ["a b", "c"]


def test_empty_args(tmp_path):
    assert _run(tmp_path, "") == []
    assert _run(tmp_path, None) == []


def test_declaration_keeps_args_as_string():
    import ast
    from pathlib import Path
    src = Path(sys.modules["jarvis.actions.code_helper"].__file__).parent.parent / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    decls = next(ast.literal_eval(s.value) for s in tree.body
                 if isinstance(s, ast.Assign) and getattr(s.targets[0], "id", "") == "TOOL_DECLARATIONS")
    code_helper_decl = next(d for d in decls if d["name"] == "code_helper")
    assert code_helper_decl["parameters"]["properties"]["args"]["type"] == "STRING"
