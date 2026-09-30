"""guncelle-32 (canlı test 2026-09-30): Groq günlük sınırı 6. görevde doldu, kalan
görevler yerel modelle yazıldı; csv_birlestir'de görevin verdiği dosyalar yerine
örnek veriyle çalışılıp 'çalışıyor' denildi (yalancı başarı)."""
from __future__ import annotations

import pytest

from jarvis.actions import dev_agent as da


class _Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text, self.headers = code, data, text, {}

    def json(self):
        return self._d


class _Local:
    def generate_content(self, c):
        return da._GeminiCliResponse("yerel")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(da, "_CLOUD_OFF_UNTIL", 0.0)
    monkeypatch.setattr(da, "_CLOUD_MODEL_OFF", {})
    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    monkeypatch.delenv("JARVIS_DEVAGENT_BACKEND", raising=False)
    for k in ("JARVIS_CLOUD_LLM_MODEL", "JARVIS_CLOUD_LLM_MODELS", "JARVIS_CLOUD_LLM_URL"):
        monkeypatch.delenv(k, raising=False)
    import jarvis.core.secure_config as sc
    monkeypatch.setattr(sc, "load_config", lambda: {})
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testanahtar12345")


def test_daily_limit_switches_to_next_groq_model(monkeypatch, capsys):
    import requests
    seen = []

    def fake(url, json=None, headers=None, timeout=0):
        seen.append(json["model"])
        if json["model"] == "openai/gpt-oss-120b":
            return _Resp(429, text="Rate limit reached ... tokens per day (TPD)")
        return _Resp(200, {"choices": [{"message": {"content": "<think>hmm</think>\nKOD"}}]})
    monkeypatch.setattr(requests, "post", fake)
    url, model, key = da._cloud_llm()
    assert model == "openai/gpt-oss-120b"
    r = da._CloudLLM(url, model, key, fallback=_Local()).generate_content("yaz")
    assert r.text == "KOD" and seen == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    assert "qwen/qwen3.8-27b modeline geçiliyor" in capsys.readouterr().out
    assert da._cloud_llm()[1] == "qwen/qwen3.8-27b" and da._CLOUD_OFF_UNTIL == 0.0


def test_all_models_exhausted_means_local(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(429, text="tokens per day (TPD)"))
    url, model, key = da._cloud_llm()
    assert da._CloudLLM(url, model, key, fallback=_Local()).generate_content("x").text == "yerel"
    assert da._cloud_llm() is None


def test_background_prefers_small_model():
    assert da._cloud_llm(prefer=da.CLOUD_LLM_BACKGROUND_MODEL)[1] == "openai/gpt-oss-20b"


def test_other_provider_keeps_single_model(monkeypatch):
    monkeypatch.setenv("JARVIS_CLOUD_LLM_URL", "https://baska.example/v1")
    monkeypatch.setenv("JARVIS_CLOUD_LLM_MODEL", "benim-modelim")
    assert da._cloud_models({}, "https://baska.example/v1") == ["benim-modelim"]


def test_task_input_paths_are_added_to_run_command(tmp_path):
    k = tmp_path / "_girdi"
    k.mkdir()
    (k / "musteriler.csv").write_text("id,ad\n1,Ayşe\n", encoding="utf-8")
    (k / "siparisler.csv").write_text("musteri_id,tutar\n1,10\n", encoding="utf-8")
    desc = (f"{k}/musteriler.csv (id, ad) ve {k}/siparisler.csv dosyalarını birleştir ... "
            f"(run_command: python main.py {k}/musteriler.csv {k}/siparisler.csv). "
            f"Sonuç https://ornek.com/a/b sayfasına değil /yok/boyle/bir/yol dosyasına değil.")
    plan = da._ensure_paths_in_run_command({"run_command": "python main.py"}, desc)
    assert plan["run_command"] == f"python main.py {k}/musteriler.csv {k}/siparisler.csv"
    same = da._ensure_paths_in_run_command({"run_command": plan["run_command"]}, desc)
    assert same["run_command"] == plan["run_command"]


def test_output_preview_shows_real_error_line():
    tb = ("STDERR:\nTraceback (most recent call last):\n  File \"/x/main.py\", line 32, in <module>\n    main()\n"
          "  File \"/x/main.py\", line 25, in main\n    generate_chart()\n"
          "TypeError: generate_chart() missing 1 required positional argument: 'stats'\n")
    prev = da._output_preview(tb)
    assert "TypeError: generate_chart() missing 1 required positional argument" in prev
    assert da._output_preview("STDOUT:\nkısa çıktı") == "STDOUT:\nkısa çıktı"


def test_call_signature_mismatch_found_across_files():
    codes = {
        "core/report.py": "def compute(db):\n    return {}\n\ndef generate_chart(stats, output_path='g.png'):\n    pass\n",
        "main.py": ("from core.report import compute, generate_chart\n"
                    "def main():\n    s = compute(1)\n    generate_chart()\n    generate_chart(s, 'a', 'b')\n"
                    "    generate_chart(s, renk='k')\n    generate_chart(stats=s)\n    generate_chart(*[s])\n"),
    }
    found = da._call_mismatches(codes)
    msgs = [i["message"] for i in found["main.py"]]
    lines = [i["line"] for i in found["main.py"]]
    assert lines == [4, 5, 6], msgs
    assert "missing required argument(s): stats" in msgs[0]
    assert "at most 2" in msgs[1] and "renk" in msgs[2]
    assert "core/report.py" in da._call_mismatch_hint(codes)
    assert da._call_mismatches({"main.py": "print(1)\n"}) == {}
