"""Sürpriz görev doğrulayıcıları: boş çıktıyı reddetmeli, doğru çıktıyı kabul etmeli."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import canli_test_surpriz as S  # noqa: E402

G = {g["no"]: g for g in S.SURPRIZ}


def test_pool_is_well_formed():
    nos = [g["no"] for g in S.SURPRIZ]
    assert len(nos) == len(set(nos)) >= 10
    for g in S.SURPRIZ:
        assert g["dil"] in ("python", "go") and callable(g["dogrula"]) and g["tarif"]


@pytest.mark.parametrize("no", [g["no"] for g in S.SURPRIZ])
def test_empty_output_is_rejected(no, tmp_path):
    g = G[no]
    if g["hazirla"]:
        (tmp_path / "girdi").mkdir()
        g["hazirla"](tmp_path / "girdi")
    with pytest.raises(AssertionError):
        g["dogrula"](tmp_path / "proje")


def test_correct_outputs_are_accepted(tmp_path):
    (tmp_path / "durum.json").write_text(json.dumps({"200": 3, "404": 2, "500": 1}))
    G[103]["dogrula"](tmp_path)
    (tmp_path / "ozet.txt").write_text("en düşük: 26.75\nen yüksek: 35.25\nortalama: 30.38\n", encoding="utf-8")
    G[106]["dogrula"](tmp_path)
    (tmp_path / "istatistik.json").write_text(json.dumps({"satir": 4, "kelime": 10}))
    G[109]["dogrula"](tmp_path)
    (tmp_path / "uzantilar.txt").write_text(".txt: 3\n.go: 2\n.md: 1\n")
    G[111]["dogrula"](tmp_path)


def test_unexpected_output_shape_does_not_abort_the_run(tmp_path, monkeypatch):
    """PR #20 kod incelemesi: doğrulayıcıdan TypeError/ValueError tüm turu durdurmamalı."""
    import canli_test as ct

    monkeypatch.setattr(ct.da, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(ct.da, "_build_project", lambda **k: "Project 'x' is working")

    def boom(proje):
        raise TypeError("'NoneType' object is not subscriptable")

    sonuc = ct.calistir({"no": 999, "ad": "patlayan", "dil": "python", "hazirla": None,
                         "dogrula": boom, "tarif": "x"}, tmp_path)
    assert sonuc["durum"] == "YALANCI BAŞARI" and "TypeError" in sonuc["neden"]


def test_ozet_picks_the_real_error_line():
    import canli_test as ct
    s = ("Project couldn't be verified, sir. Project is saved at /x/y — open it in VSCode.\n\n"
         "Last error:\nACCEPTANCE TEST FAILED:\n'quotes.csv' kabul testinde oluşturulmadı.\n")
    assert ct._ozet(s).startswith("'quotes.csv' kabul testinde oluşturulmadı")
    assert "Planning failed" in ct._ozet("Planning failed: bad json")


def test_live_test_refuses_to_start_on_low_disk(monkeypatch, capsys):
    from collections import namedtuple

    import canli_test as ct
    monkeypatch.setattr(ct.shutil, "disk_usage", lambda p: namedtuple("u", "total used free")(10, 9, 2**29))
    monkeypatch.setattr(sys, "argv", ["canli_test.py", "--surpriz", "0"])
    assert ct.main() == 2 and "boş alan" in capsys.readouterr().out
