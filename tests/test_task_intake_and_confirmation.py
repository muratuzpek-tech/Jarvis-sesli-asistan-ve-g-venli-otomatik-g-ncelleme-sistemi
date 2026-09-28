"""Görev kabulü (eksik bilgi, tarayıcı gereksinimi), modelden bağımsız onay
kapısı, arka plan görevleri ve web kazıyıcı kabul testi.

Kaynak: 2026-09-28 Windows canlı testi — "[Hedef URL]" şablonuyla proje
planlandı, requests seçildi ve Gemini onay beklemeden confirm_code kullandı.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import jarvis.actions.dev_agent as da
from jarvis.actions.devkit.task_intake import (
    find_placeholders,
    missing_inputs,
    needs_browser,
    plan_uses_browser,
)

LIVE_REQUEST = (
    "Jarvis, [Hedef URL] adresine git. Sayfa tamamen yüklendikten ve tüm JavaScript scriptleri "
    "çalıştırıldıktan sonra verileri kazı. Özellikle 'Daha Fazla Yükle' (Load More) butonuna veya "
    "sayfa aşağı kaydırıldıkça (infinite scroll) yüklenen içeriklere odaklan. İlk 20 girdinin "
    "başlığını, yayınlanma tarihini ve içerik özetini topla. Çıktıyı [ {id, baslik, tarih, ozet} ] "
    "şeklinde yapılandırılmış JSON olarak teslim et."
)
GOOD_REQUEST = LIVE_REQUEST.replace("[Hedef URL]", "https://quotes.toscrape.com/scroll")


# ── task_intake ──────────────────────────────────────────────────────────
def test_live_placeholder_is_found_but_json_schema_is_not():
    assert find_placeholders(LIVE_REQUEST) == ["[Hedef URL]"]
    assert find_placeholders(GOOD_REQUEST) == []


@pytest.mark.parametrize("text", ["<dosya yolu> içindeki satırları say", "{klasor_adi} altını tara",
                                  "YOUR_API_KEY ile bağlan"])
def test_other_placeholder_shapes(text):
    assert find_placeholders(text)


def test_missing_inputs():
    assert missing_inputs(LIVE_REQUEST)
    assert missing_inputs(GOOD_REQUEST) == []
    assert missing_inputs("haber sitesinin adresine git ve başlıkları kazı")
    assert missing_inputs("hurriyet.com.tr adresindeki başlıkları kazı") == []
    assert missing_inputs("bir klasördeki txt dosyalarındaki kelimeleri sayan program yaz") == []


def test_needs_browser():
    assert needs_browser(GOOD_REQUEST)
    assert needs_browser("sayfayı aşağı kaydırarak ilk 20 alıntıyı topla")
    assert not needs_browser("CSV dosyasındaki satırları topla ve rapor yaz")
    assert plan_uses_browser({"dependencies": ["playwright>=1.40"]})
    assert not plan_uses_browser({"dependencies": ["requests", "beautifulsoup4"]})


# ── dev_agent: eksik bilgi ve onay kapısı ─────────────────────────────────
@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(da, "_pending_dev_agent", {})
    monkeypatch.setattr(da, "_last_user_turn_at", None)
    built = []
    monkeypatch.setattr(da, "_build_project", lambda **kw: built.append(kw) or "Project 'x' is working")
    return built


def _code(preview: str) -> str:
    return preview.split("confirm_code='")[1].split("'")[0]


def test_placeholder_request_is_not_planned(gate):
    result = da.dev_agent({"description": LIVE_REQUEST})
    assert result.startswith("BİLGİ EKSİK") and "[Hedef URL]" in result
    assert da._pending_dev_agent == {} and gate == []


def test_confirm_code_rejected_without_user_reply(gate):
    da.note_user_turn()                      # kullanıcının İSTEK cümlesi
    code = _code(da.dev_agent({"description": GOOD_REQUEST}))
    result = da.dev_agent({"description": GOOD_REQUEST, "confirm_code": code})  # model aynı turda zincirledi
    assert result.startswith("ONAY HENÜZ ALINMADI") and gate == []
    assert code in da._pending_dev_agent     # kod geçerli kalır; kullanıcı onaylayınca kullanılabilir


def test_late_transcript_of_request_is_not_confirmation(gate):
    code = _code(da.dev_agent({"description": GOOD_REQUEST}))
    da.note_user_turn(da._pending_dev_agent[code]["issued_at"] + 0.3)  # gecikmeli ses-yazı parçası
    assert da.dev_agent({"description": GOOD_REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")


def test_confirm_code_accepted_after_user_reply(gate):
    code = _code(da.dev_agent({"description": GOOD_REQUEST}))
    da.note_user_turn(time.monotonic() + 5)  # kullanıcı "evet" dedi
    assert "is working" in da.dev_agent({"description": GOOD_REQUEST, "confirm_code": code})
    assert len(gate) == 1 and code not in da._pending_dev_agent


# ── planlayıcı: JS isteyen görevde tarayıcı zorunlu ───────────────────────
def _plan_json(deps):
    return json.dumps({"project_name": "s", "entry_point": "main.py",
                       "files": [{"path": "main.py", "imports": []}],
                       "run_command": "python main.py https://quotes.toscrape.com/scroll",
                       "dependencies": deps, "expected_outputs": []})


class _SeqModel:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate_content(self, prompt):
        self.prompts.append(prompt)
        r = type("R", (), {})()
        r.text = self.replies.pop(0)
        return r


def test_planner_is_reprompted_until_it_uses_playwright(monkeypatch):
    model = _SeqModel([_plan_json(["requests"]), _plan_json(["playwright"])])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    plan = da._plan_project(GOOD_REQUEST, "python")
    assert plan["dependencies"] == ["playwright"]
    assert len(model.prompts) == 2 and "BROWSER REQUIRED" in model.prompts[1]


def test_planner_refusing_browser_fails_loudly(monkeypatch):
    model = _SeqModel([_plan_json(["requests"]), _plan_json(["requests", "bs4"])])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    with pytest.raises(ValueError, match="Playwright"):
        da._plan_project(GOOD_REQUEST, "python")


def test_static_task_is_not_reprompted(monkeypatch):
    model = _SeqModel([_plan_json(["requests"])])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    da._plan_project("https://example.com sayfasının başlığını yaz", "python")
    assert len(model.prompts) == 1


# ── arka plan görevleri ───────────────────────────────────────────────────
@pytest.fixture
def board(tmp_path, monkeypatch, gate):
    import jarvis.actions.agent_board as ab
    monkeypatch.setattr(ab, "DB_PATH", tmp_path / "board.db")
    return ab


def _jobs(ab):
    with sqlite3.connect(ab.DB_PATH) as c:
        return c.execute("SELECT status, result FROM jobs").fetchall()


def test_parallel_task_needs_confirmation_first(board):
    preview = board.start_parallel_task({"description": GOOD_REQUEST})
    assert "confirm_code='" in preview and "start_parallel_task" in preview
    assert not board.DB_PATH.exists() or _jobs(board) == []


def test_parallel_task_rejects_bad_code(board):
    assert board.start_parallel_task({"description": GOOD_REQUEST, "confirm_code": "nope"}).startswith("Onay kodu")


def test_parallel_task_runs_after_confirmation(board, gate):
    code = _code(board.start_parallel_task({"description": GOOD_REQUEST}))
    da.note_user_turn(time.monotonic() + 5)
    msg = board.start_parallel_task({"description": GOOD_REQUEST, "confirm_code": code})
    assert "arka planda başlatıldı" in msg
    for _ in range(100):
        if _jobs(board) and _jobs(board)[0][0] in ("completed", "failed"):
            break
        time.sleep(0.05)
    assert _jobs(board)[0][0] == "completed" and len(gate) == 1


def test_background_job_is_not_completed_when_nothing_was_built(board, monkeypatch):
    monkeypatch.setattr(da, "dev_agent", lambda parameters: "ONAY GEREKLİ: ...")
    board._run_job_in_background("j1", "x", "python", "", "")
    assert not board.DB_PATH.exists() or all(s != "completed" for s, _ in _jobs(board))


# ── kabul testi: yerel HTML ile web kazıyıcı ──────────────────────────────
def test_acceptance_fixture_url(tmp_path):
    from jarvis.actions.devkit.acceptance import contract_text, run_acceptance, validate_spec

    spec = {"applicable": True,
            "fixtures": [{"path": "page.html", "content": "<h2>Birinci</h2><h2>Ikinci</h2>"}],
            "args": ["{FIXTURE_URL}/page.html"],
            "expect": [{"output": "out.json", "contains": ["Birinci", "Ikinci"]}]}
    clean, why = validate_spec(spec)
    assert clean, why
    assert "file:///<sample_folder>/page.html" in contract_text(clean)
    assert validate_spec({**spec, "args": ["{FIXTURE_URL}/../../etc/passwd"]})[0] is None

    (tmp_path / "main.py").write_text(
        "import json, re, sys, urllib.parse, urllib.request\n"
        "url = sys.argv[1]\n"
        "assert url.startswith('file://'), url\n"
        "html = urllib.request.urlopen(url).read().decode()\n"
        "json.dump(re.findall(r'<h2>(.*?)</h2>', html), open('out.json', 'w'))\n",
        encoding="utf-8",
    )
    problems, output = run_acceptance(tmp_path, "main.py", clean)
    assert problems == [], (problems, output)


def test_open_editor_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DEVAGENT_OPEN_EDITOR", "0")
    launched = []
    monkeypatch.setattr(da.subprocess, "Popen", lambda *a, **k: launched.append(a))
    assert da._open_vscode(tmp_path) is False and launched == []
