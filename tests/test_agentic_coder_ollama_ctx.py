"""tests/test_agentic_coder_ollama_ctx.py — Ollama baglam boyutu + qwen3 dusunme

- JARVIS_OLLAMA_NUM_CTX (varsayilan 4096, gecersizde varsayilan) ollama.chat
  options'a num_ctx olarak gider (eskiden 16384 sabitti).
- Model adi "qwen3" ile basliyorsa think=False gonderilir ve yanittaki
  <think>...</think> bloklari AYRISTIRMADAN ONCE temizlenir; diger modellerde
  istek ve yanit degismez.
- Kullanilan model + num_ctx canli durum kaydina (live_status model alani) ve
  ekrandaki ILK ilerleme satirina yazilir.
- run_stats(): son kosunun tur/ret/hata kategorisi ozeti (olcum betigi icin).

Ollama paketi sys.modules'te kayit tutan sahte bir modulle degistirilir;
gercek Ollama'ya baglanilmaz. HOME tmp_path altindadir. solve() gercek
AgenticCoder dongusudur.
"""
import asyncio
import json
import sys
import types

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac

from tests.test_agentic_coder_live import MAIN, README, _decide, _write


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ac._LIVE.clear()
    monkeypatch.setattr(ac, "_last_model", None)
    monkeypatch.setattr(ac, "_gemini_api_key", lambda: "")
    monkeypatch.delenv("JARVIS_OLLAMA_NUM_CTX", raising=False)
    yield
    ac._LIVE.clear()


class _RespErr(Exception):
    pass


def _fake_ollama(monkeypatch, replies, calls):
    """Senaryolu ollama.chat: her cagrinin kwargs'ini kaydeder, sirayla yanit verir."""
    seq = iter(replies)

    def chat(**kw):
        calls.append(kw)
        return {"message": {"content": next(seq, _decide({"action": "inspect", "args": {}}))}}

    monkeypatch.setitem(sys.modules, "ollama", types.SimpleNamespace(ResponseError=_RespErr, chat=chat))


# ── 1) num_ctx ortam degiskeni ──

@pytest.mark.parametrize("value, expected", [
    (None, 4096), ("8192", 8192), (" 2048 ", 2048),
    ("abc", 4096), ("0", 4096), ("-5", 4096), ("", 4096), ("4096.5", 4096),
])
def test_num_ctx_from_env_reaches_ollama_chat(monkeypatch, value, expected):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen2.5-coder:7b")
    if value is not None:
        monkeypatch.setenv("JARVIS_OLLAMA_NUM_CTX", value)
    calls: list[dict] = []
    _fake_ollama(monkeypatch, ['{"action": "inspect"}'], calls)
    assert ac.AgenticCoder._default_model("merhaba") == '{"action": "inspect"}'
    assert calls[0]["options"]["num_ctx"] == expected
    assert ac.last_model() == {"provider": "ollama", "name": "qwen2.5-coder:7b", "num_ctx": expected}


# ── 2) qwen3: think=False + <think> temizligi; digerleri degismez ──

THINK = '<think>\nOnce {"action": "write"} dusundum; sonra } ve { yazdim.\n</think>\n\n'


def test_qwen3_disables_thinking_and_strips_think_blocks(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen3:14b")
    calls: list[dict] = []
    real = _decide({"action": "inspect", "args": {"target": "."}})
    _fake_ollama(monkeypatch, [THINK + real], calls)
    raw = ac.AgenticCoder._default_model("merhaba")
    assert calls[0]["think"] is False
    assert "<think>" not in raw
    assert ac._parse_model_response(raw) == json.loads(real)


def test_qwen3_dangling_close_tag_is_stripped(monkeypatch):
    # Sablon <think>'i prompt'a koydugunda yanit yalnizca '</think>' ile gelir.
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "Qwen3-Coder:30b")
    calls: list[dict] = []
    real = _decide({"action": "accept", "args": {}})
    _fake_ollama(monkeypatch, ['dusunce { yarim\n</think>\n' + real], calls)
    raw = ac.AgenticCoder._default_model("merhaba")
    assert ac._parse_model_response(raw) == json.loads(real)


def test_qwen3_old_client_without_think_uses_no_think_prompt(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen3:8b")
    calls: list[dict] = []

    def chat(**kw):
        calls.append(kw)
        if "think" in kw:
            raise TypeError("chat() got an unexpected keyword argument 'think'")
        return {"message": {"content": THINK + '{"action": "inspect"}'}}

    monkeypatch.setitem(sys.modules, "ollama", types.SimpleNamespace(ResponseError=_RespErr, chat=chat))
    assert ac.AgenticCoder._default_model("merhaba") == '{"action": "inspect"}'
    assert "think" not in calls[-1]
    assert calls[-1]["messages"][-1]["content"].rstrip().endswith("/no_think")


def test_other_models_unchanged(monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen2.5-coder:7b")
    calls: list[dict] = []
    _fake_ollama(monkeypatch, [THINK + '{"action": "inspect"}'], calls)
    raw = ac.AgenticCoder._default_model("merhaba")
    assert raw == THINK + '{"action": "inspect"}'          # yanit oldugu gibi
    assert "think" not in calls[0]
    assert calls[0]["messages"] == [{"role": "user", "content": "merhaba"}]
    assert calls[0]["format"] == "json"


# ── 3) canli durum + ilk ilerleme satiri (gercek solve) ──

class _UI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, msg):
        self.lines.append(msg)


def test_solve_reports_model_and_ctx_first_and_in_live_status(home, monkeypatch):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen3:14b")
    monkeypatch.setenv("JARVIS_OLLAMA_NUM_CTX", "6144")
    seen: list[dict] = []
    calls: list[dict] = []
    replies = [THINK + _write("main.py", MAIN), THINK + _write("README.md", README),
               THINK + _decide({"action": "accept", "args": {}, "response": "bitti"})]
    _fake_ollama(monkeypatch, replies, calls)
    real_chat = sys.modules["ollama"].chat

    def chat(**kw):
        seen.append(ac.live_status()[0]["model"])          # tur basi yayimlandi
        return real_chat(**kw)

    sys.modules["ollama"].chat = chat
    ui = _UI()
    coder = ac.AgenticCoder(ui=ui, max_iterations=6)
    result = asyncio.run(coder.solve(description="hesap makinesi yaz",
                                     project_path=str(home / "jarvis_programs" / "hesap")))
    assert "Durum: BAŞARILI" in result, result
    assert ui.lines[0] == "🧠 ollama qwen3:14b ctx=6144"
    want = {"provider": "ollama", "name": "qwen3:14b", "num_ctx": 6144}
    assert seen[0] == want                                  # ilk cagridan ONCE de bilinir
    assert ac.live_status()[0]["model"] == want
    assert all(c["think"] is False and c["options"]["num_ctx"] == 6144 for c in calls)


def test_custom_model_fn_first_line_and_live_model(home):
    ui = _UI()
    coder = ac.AgenticCoder(model_fn=lambda p: _decide({"action": "inspect", "args": {}}),
                            ui=ui, max_iterations=1)
    asyncio.run(coder.solve(description="hesap makinesi yaz",
                            project_path=str(home / "jarvis_programs" / "hesap")))
    assert ui.lines[0] == "🧠 özel model_fn"
    assert ac.live_status()[0]["model"] == {"provider": "özel", "name": "", "num_ctx": 0}


# ── 4) run_stats ──

def test_run_stats_counts_iterations_and_reject_categories(home):
    evil = MAIN.replace("return a + b", 'return eval(f"{a} + {b}")')
    broken = MAIN.replace("def main() -> None:", "def main( -> None:")
    seq = iter([_write("main.py", evil), _write("main.py", broken),
                _write("main.py", MAIN), _write("README.md", README),
                _decide({"action": "accept", "args": {}, "response": "bitti"})])
    coder = ac.AgenticCoder(model_fn=lambda p: next(seq, _decide({"action": "inspect", "args": {}})),
                            max_iterations=8)
    assert coder.run_stats() is None
    result = asyncio.run(coder.solve(description="hesap makinesi yaz",
                                     project_path=str(home / "jarvis_programs" / "hesap")))
    assert "Durum: BAŞARILI" in result, result
    stats = coder.run_stats()
    assert stats["accepted"] is True
    assert stats["iterations"] == 5
    assert stats["rejects"] == 2
    assert stats["error_categories"] == {"EVAL": 1, "SYNTAX": 1}
