"""Karmaşık görev doğrulayıcıları: boş çıktıyı reddetmeli, doğru çözümü kabul
etmeli, tipik YANLIŞ çözümleri (yalnız üst klasör, .txt'yi de sayma, tekrarları
atmama...) reddetmeli. Doğru çözümler gerçek program olarak çalıştırılır."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import canli_test_karmasik as K  # noqa: E402

G = {g["no"]: g for g in K.KARMASIK}


def test_pool_is_well_formed():
    nos = [g["no"] for g in K.KARMASIK]
    assert len(nos) == len(set(nos)) >= 4 and min(nos) > 200
    for g in K.KARMASIK:
        assert g["dil"] == "python" and callable(g["dogrula"]) and g["tarif"]


@pytest.mark.parametrize("no", [g["no"] for g in K.KARMASIK])
def test_empty_output_is_rejected(no, tmp_path):
    g = G[no]
    if g["hazirla"]:
        (tmp_path / "girdi").mkdir()
        g["hazirla"](tmp_path / "girdi")
    (tmp_path / "proje").mkdir()
    with pytest.raises(AssertionError):
        g["dogrula"](tmp_path / "proje")


def _run(code: str, proje: Path, *args: str) -> None:
    proje.mkdir(parents=True, exist_ok=True)
    (proje / "main.py").write_text(code, encoding="utf-8")
    subprocess.run([sys.executable, "main.py", *args], cwd=proje, check=True, timeout=60)


# ── 201 (ağ gerektirir; testte sahte ama tutarlı çıktı) ────────────────────
def _kitap_ciktisi(p: Path, rows: list[tuple[str, float]], html_rows: list[tuple[str, float]]) -> None:
    p.mkdir(exist_ok=True)
    con = sqlite3.connect(p / "ucuz_kitaplar.db")
    con.execute("CREATE TABLE kitaplar (ad TEXT, fiyat REAL)")
    con.executemany("INSERT INTO kitaplar VALUES (?, ?)", rows)
    con.commit()
    con.close()
    tr = "".join(f"<tr><td>{a}</td><td>£{f:.2f}</td></tr>" for a, f in html_rows)
    (p / "en_ucuz_5.html").write_text(f"<table><tr><th>Ad</th><th>Fiyat</th></tr>{tr}</table>", encoding="utf-8")


KITAPLAR = [("A Light in the Attic Deluxe", 13.99), ("Sharp Objects", 17.93), ("Olio", 12.84),
            ("Mesaerion", 15.94), ("Libertarianism for Beginners", 13.12), ("It's Only the Himalayas", 19.83)]


def test_201_accepts_consistent_output(tmp_path):
    _kitap_ciktisi(tmp_path, KITAPLAR, sorted(KITAPLAR, key=lambda x: x[1])[:5])
    G[201]["dogrula"](tmp_path)


@pytest.mark.parametrize("bozuk", ["pahali", "kisaltilmis", "yanlis_bes"])
def test_201_rejects_wrong_output(tmp_path, bozuk):
    rows = list(KITAPLAR)
    html = sorted(rows, key=lambda x: x[1])[:5]
    if bozuk == "pahali":
        rows.append(("Tipping the Velvet", 53.74))
    elif bozuk == "kisaltilmis":
        rows[0] = ("A Light in the ...", 13.99)
    else:
        html = sorted(rows, key=lambda x: x[1])[1:6]   # en ucuzu atlanmış
    _kitap_ciktisi(tmp_path, rows, html)
    with pytest.raises(AssertionError):
        G[201]["dogrula"](tmp_path)


# ── 202 ────────────────────────────────────────────────────────────────────
COZUM_202 = r'''
import csv, sys
from collections import Counter
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
c = Counter()
for p in sorted(q for q in Path(sys.argv[1]).rglob("*{ext}") if q.is_file()):
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "ERROR":
            c[int(parts[1][:2])] += 1
with open("hata_saatleri.csv", "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["saat", "adet"]); w.writerows(sorted(c.items()))
plt.bar([str(h) for h in sorted(c)], [c[h] for h in sorted(c)]); plt.savefig("hata_grafigi.png")
'''


@pytest.mark.parametrize("ext,dogru", [(".log", True), ("", False)])   # "" → .txt'yi de sayar
def test_202_reference_solution(tmp_path, ext, dogru):
    pytest.importorskip("matplotlib")
    K._h_log_saatlik(tmp_path)
    _run(COZUM_202.replace("{ext}", ext), tmp_path / "proje", str(tmp_path / "loglar"))
    if dogru:
        G[202]["dogrula"](tmp_path / "proje")
    else:
        with pytest.raises(AssertionError):
            G[202]["dogrula"](tmp_path / "proje")


def test_202_rejects_top_level_only(tmp_path):
    p = tmp_path / "proje"
    p.mkdir()
    (p / "hata_saatleri.csv").write_text("saat,adet\n9,2\n10,1\n", encoding="utf-8")  # eski/ atlanmış
    with pytest.raises(AssertionError):
        G[202]["dogrula"](p)


# ── 203 ────────────────────────────────────────────────────────────────────
COZUM_203 = r'''
import csv, json, sys, zipfile
musteri = {r["id"]: r["ad"] for r in csv.DictReader(open(sys.argv[1], encoding="utf-8"))}
seen, temiz = set(), []
for r in csv.DictReader(open(sys.argv[2], encoding="utf-8")):
    if {dedupe} and r["siparis_no"] in seen:
        continue
    seen.add(r["siparis_no"])
    if r["tutar"].strip() and r["musteri_id"] in musteri:
        temiz.append(r)
with open("temiz_siparisler.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["siparis_no", "musteri_id", "tutar"]); w.writeheader(); w.writerows(temiz)
rapor = {}
for r in temiz:
    d = rapor.setdefault(musteri[r["musteri_id"]], {"siparis": 0, "toplam": 0.0})
    d["siparis"] += 1; d["toplam"] += float(r["tutar"])
json.dump(rapor, open("musteri_raporu.json", "w", encoding="utf-8"), ensure_ascii=False)
with zipfile.ZipFile("rapor_yedek.zip", "w") as z:
    z.write("temiz_siparisler.csv"); z.write("musteri_raporu.json")
'''


@pytest.mark.parametrize("dedupe,dogru", [("True", True), ("False", False)])
def test_203_reference_solution(tmp_path, dedupe, dogru):
    K._h_temizlik(tmp_path)
    _run(COZUM_203.replace("{dedupe}", dedupe), tmp_path / "proje",
         str(tmp_path / "musteriler.csv"), str(tmp_path / "siparisler.csv"))
    if dogru:
        G[203]["dogrula"](tmp_path / "proje")
    else:
        with pytest.raises(AssertionError):
            G[203]["dogrula"](tmp_path / "proje")


def test_203_rejects_missing_zip_member(tmp_path):
    K._h_temizlik(tmp_path)
    _run(COZUM_203.replace("{dedupe}", "True"), tmp_path / "proje",
         str(tmp_path / "musteriler.csv"), str(tmp_path / "siparisler.csv"))
    with zipfile.ZipFile(tmp_path / "proje" / "rapor_yedek.zip", "w") as z:
        z.write(tmp_path / "proje" / "musteri_raporu.json", "musteri_raporu.json")
    with pytest.raises(AssertionError):
        G[203]["dogrula"](tmp_path / "proje")


# ── 204 ────────────────────────────────────────────────────────────────────
COZUM_204 = r'''
import re, sys
from pathlib import Path
root = Path(sys.argv[1]); out = ["# Yapılacaklar", ""]; n = 0
for p in sorted(root.rglob("{glob}")):
    if not p.is_file():
        continue
    notes = [(i, m.group(0)) for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             for m in [re.search(r"(TODO|FIXME).*", line)] if m]
    if notes:
        out.append(f"## {p.relative_to(root)}")
        out += [f"- satır {i}: {t}" for i, t in notes]; out.append(""); n += len(notes)
out.append(f"**Toplam: {n} not**")
Path("yapilacaklar.md").write_text("\n".join(out) + "\n", encoding="utf-8")
'''


@pytest.mark.parametrize("glob,dogru", [("*.py", True), ("*", False)])   # "*" → .txt'yi de alır
def test_204_reference_solution(tmp_path, glob, dogru):
    K._h_todo(tmp_path)
    _run(COZUM_204.replace("{glob}", glob), tmp_path / "cikti", str(tmp_path / "proje"))
    if dogru:
        G[204]["dogrula"](tmp_path / "cikti")
    else:
        with pytest.raises(AssertionError):
            G[204]["dogrula"](tmp_path / "cikti")


def test_outputs_in_subfolder_are_found(tmp_path):
    """Doğrulayıcı proje içinde alt klasöre yazılmış çıktıyı da bulur; .jarvis'i yok sayar."""
    K._h_todo(tmp_path)
    _run(COZUM_204.replace("{glob}", "*.py"), tmp_path / "cikti" / "out", str(tmp_path / "proje"))
    G[204]["dogrula"](tmp_path / "cikti")
