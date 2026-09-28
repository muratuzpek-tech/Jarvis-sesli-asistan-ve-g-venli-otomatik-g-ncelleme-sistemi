"""Plan'daki beklenen çıktı bir KLASÖR olabilir (dosyaları alt klasörlere ayıran görevler)."""
from __future__ import annotations

import os
import shutil
import time

from jarvis.actions import dev_agent as da


def test_folder_with_fresh_files_passes(tmp_path):
    t0 = time.time()
    src = tmp_path / "eski.jpg"
    src.write_text("x")
    os.utime(src, (1_700_000_000, 1_700_000_000))           # 2023 tarihli dosya
    (tmp_path / "out" / "2023-11").mkdir(parents=True)
    shutil.copy2(src, tmp_path / "out" / "2023-11" / "eski.jpg")   # copy2 eski mtime'ı korur
    assert da._check_expected_outputs(tmp_path, [{"path": "out"}], t0) == []


def test_empty_folder_is_reported(tmp_path):
    (tmp_path / "out").mkdir()
    problems = da._check_expected_outputs(tmp_path, [{"path": "out/"}], time.time())
    assert len(problems) == 1 and "contains no files" in problems[0]


def test_missing_folder_is_reported(tmp_path):
    problems = da._check_expected_outputs(tmp_path, [{"path": "out"}], time.time())
    assert problems == ["'out' was never created."]
