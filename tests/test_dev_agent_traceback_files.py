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
