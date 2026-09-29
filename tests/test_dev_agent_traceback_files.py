"""Aynı hata tekrarlanınca traceback'teki TÜM proje dosyaları düzeltilir."""
from jarvis.actions import dev_agent as da

TB = '''Traceback (most recent call last):
  File "/p/js/main.py", line 29, in <module>
    quotes = scrape(url)
  File "/p/js/utils/helpers.py", line 12, in scrape
    page.wait_for_selector(".quote")
  File "/venv/lib/python3.12/site-packages/playwright/sync_api/_generated.py", line 8000, in wait_for_selector
playwright._impl._errors.TimeoutError: Timeout 30000ms exceeded.
'''


def test_all_project_frames_are_listed_deepest_first():
    files = ["main.py", "utils/helpers.py", "utils/other.py"]
    assert da._traceback_project_files(TB, files) == ["utils/helpers.py", "main.py"]
    assert da._parse_traceback(TB, files)[0] == "utils/helpers.py"   # tekrarsız durumda eskisi gibi


def test_undefined_name_is_located_even_without_traceback():
    codes = {
        "main.py": "from utils.helpers import copy_all\nimport sys\n\ntry:\n    copy_all(sys.argv[1])\n"
                   "except Exception as e:\n    print(f'Hata: {e}')\n",
        "utils/helpers.py": "import shutil\n\ndef copy_all(folder):\n    for p in []:\n        shutil.copy2(src, p)\n",
    }
    note, files = da._undefined_name_note("Hata: name 'src' is not defined", codes)
    assert files == ["utils/helpers.py"] and "line(s) [5]" in note
    assert da._undefined_name_note("başka bir hata", codes) is None
