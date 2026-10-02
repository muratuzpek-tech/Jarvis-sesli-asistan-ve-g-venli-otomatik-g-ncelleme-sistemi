"""Görevde adı verilen çıktı dosyası plandan eksikse eklenir (canlı test 2026-09-29, link_kontrol)."""
import json
from pathlib import Path

from jarvis.actions import dev_agent as da
from jarvis.actions.devkit.python_quality import empty_data_outputs, header_only_outputs

LINK = ("https://quotes.toscrape.com/ sayfasındaki bütün bağlantıları bulup ... her bağlantıyı "
        "çalışma klasöründeki linkler.txt dosyasına yaz.")


def test_missing_described_output_is_added():
    plan = {"run_command": "python main.py https://quotes.toscrape.com/", "expected_outputs": []}
    plan = da._ensure_described_outputs(plan, LINK)
    assert [o["path"] for o in plan["expected_outputs"]] == ["linkler.txt"]


def test_inputs_and_existing_outputs_are_not_duplicated():
    desc = ("/home/m/x/musteriler.csv ve notlar.txt dosyasını oku; sonucu temiz.csv olarak yaz, "
            "ikisini rapor.zip içine koy (run_command: python main.py /home/m/x/musteriler.csv)")
    names = da.described_output_names(desc, "python main.py /home/m/x/musteriler.csv")
    assert names == ["temiz.csv", "rapor.zip"]
    plan = da._ensure_described_outputs({"expected_outputs": [{"path": "temiz.csv"}]}, desc)
    assert [o["path"] for o in plan["expected_outputs"]] == ["temiz.csv", "rapor.zip"]


def test_empty_json_and_header_only_csv_are_flagged(tmp_path: Path):
    (tmp_path / "q.json").write_text("[]")
    (tmp_path / "ok.json").write_text(json.dumps([{"a": 1}]))
    (tmp_path / "h.csv").write_text("ad,fiyat\n")
    probs = empty_data_outputs(tmp_path, ["q.json", "ok.json", "h.csv"])
    assert len(probs) == 2 and "q.json" in probs[0] and "h.csv" in probs[1]


def test_single_data_line_is_not_header_only(tmp_path: Path):
    (tmp_path / "o.txt").write_text("en düşük: 26.75, en yüksek: 35.25, ortalama: 30.38\n", encoding="utf-8")
    (tmp_path / "h.txt").write_text("Sıcaklık Özeti\n==========\n", encoding="utf-8")
    assert header_only_outputs(tmp_path, ["o.txt"]) == []
    assert len(header_only_outputs(tmp_path, ["h.txt"])) == 1


def test_planner_translated_name_is_replaced_by_task_name():
    """Canlı test 2026-09-29: görev tablo.html dedi, plan table.html bekledi → doğru program reddedildi."""
    desc = "/home/m/urunler.json dosyasını okuyup HTML tabloyu çalışma klasöründeki tablo.html dosyasına yaz."
    plan = {"expected_outputs": [{"path": "table.html"}, {"path": "log.txt"}]}
    plan = da._ensure_described_outputs(plan, desc)
    assert [o["path"] for o in plan["expected_outputs"]] == ["log.txt", "tablo.html"]
