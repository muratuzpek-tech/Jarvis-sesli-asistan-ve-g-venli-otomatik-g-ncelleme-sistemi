"""Murat@goxs 2026-09-29 (image_sorter): görevde klasör verilmeyince program
argümansız çalıştırılıp 5 kez 'Kullanım:' ile çıktı."""
from __future__ import annotations

import os

from jarvis.actions import dev_agent as da

SPEC = {"fixtures": [{"path": "a.jpg", "content": "x", "mtime": "2024-01-15"},
                     {"path": "alt/b.jpg", "content": "y"}],
        "args": ["{FIXTURE}"], "expect": [{"output": "sorted", "contains": ["2024-01/a.jpg"]}]}


def test_sample_input_is_written_and_passed(tmp_path):
    cmd = da._with_sample_input("python main.py", tmp_path, SPEC, log=lambda m: None)
    sample = tmp_path / da.SAMPLE_INPUT_DIR
    assert cmd == f"python main.py {sample}"
    assert (sample / "alt" / "b.jpg").read_text() == "y"
    assert os.path.getmtime(sample / "a.jpg") < os.path.getmtime(sample / "alt" / "b.jpg")


def test_existing_arguments_are_kept(tmp_path):
    assert da._with_sample_input("python main.py /home/murat/Resimler", tmp_path, SPEC,
                                 log=lambda m: None) == "python main.py /home/murat/Resimler"
    assert not (tmp_path / da.SAMPLE_INPUT_DIR).exists()


def test_url_tasks_are_not_touched(tmp_path):
    spec = dict(SPEC, args=["{FIXTURE_URL}/index.html"])
    assert da._with_sample_input("python main.py", tmp_path, spec, log=lambda m: None) == "python main.py"


def test_invented_relative_input_is_replaced_with_sample(tmp_path):
    """Windows testi 2026-09-30 (kelime_sayaci): 'python main.py input.txt', input.txt yok."""
    cmd = da._with_sample_input("python main.py input.txt", tmp_path, SPEC, log=lambda m: None)
    assert "input.txt" not in cmd and da.SAMPLE_INPUT_DIR in cmd


def test_existing_relative_input_is_kept(tmp_path):
    (tmp_path / "veri.txt").write_text("x", encoding="utf-8")
    assert da._with_sample_input("python main.py veri.txt", tmp_path, SPEC,
                                 log=lambda m: None) == "python main.py veri.txt"
    assert da._with_sample_input("python main.py --yok", tmp_path, SPEC,
                                 log=lambda m: None) == "python main.py --yok"
