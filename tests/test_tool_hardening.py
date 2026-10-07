"""(3) computer_settings sesli politika, (4) terminal salt-okunur program
dogrulamasi ve git fsmonitor, (5) tar acmada yol kacisi.

Ag yok; gercek HOME'a dokunulmaz (conftest HOME'u izole eder, dosyalar
tmp_path altinda)."""
import io
import shutil
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from jarvis import security_gate as sg
from jarvis.actions import file_processor, terminal_tool as tt
from jarvis.actions.tools_kopru import _SAFE_SETTINGS_ACTIONS


# ── 3) computer_settings ─────────────────────────────────────────────────
@pytest.mark.parametrize("action", sorted(_SAFE_SETTINGS_ACTIONS | {"restart", "shutdown", "lock", "lock_screen"}))
def test_live_computer_settings_safe_and_own_flow_actions_allowed(action):
    d = sg.authorize("computer_settings", {"action": action}, sg.Source.MODEL_LIVE)
    assert d.verdict is sg.Verdict.ALLOW


@pytest.mark.parametrize("action", ["type_text", "press_key", "enter", "open_run", "paste",
                                    "close_app", "toggle_wifi", ""])
def test_live_computer_settings_other_actions_need_approval(action):
    d = sg.authorize("computer_settings", {"action": action, "value": "rm -rf ~"}, sg.Source.MODEL_LIVE)
    assert d.verdict is sg.Verdict.NEEDS_APPROVAL


# ── 4) terminal ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("argv", [["./ls"], ["/tmp/x/git", "status"], ["bin/cat", "a"],
                                  ["/usr/bin/ls"], ["rg", "--hostname-bin=/tmp/x", "foo"],
                                  ["rg", "--hostname-bin", "/tmp/x", "foo"]])
def test_readonly_rejects_paths_and_rg_hostname_bin(argv):
    assert tt._is_readonly(argv) is False


@pytest.mark.parametrize("argv", [["ls"], ["git", "status"], ["rg", "foo"]])
def test_readonly_plain_programs_still_allowed(argv):
    assert tt._is_readonly(argv) is True


def test_git_runs_with_fsmonitor_disabled(monkeypatch):
    seen = []
    monkeypatch.setattr(tt, "_check_rate_limit", lambda: (True, ""))
    monkeypatch.setattr(tt.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    tt.terminal_tool({"command": "git status"})
    assert seen and seen[0][:3] == ["git", "-c", "core.fsmonitor=false"] and seen[0][3:] == ["status"]


# ── 5) tar acma ──────────────────────────────────────────────────────────
def _evil_tar(path):
    with tarfile.open(path, "w") as t:
        data = b"pwned"
        info = tarfile.TarInfo("../escaped.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))


def test_tar_member_cannot_escape_destination(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    arc = work / "evil.tar"
    _evil_tar(arc)
    dest = work / "out"
    result = file_processor._process_archive(arc, "extract", {"destination": str(dest)})
    assert not (work / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()
    assert not result.startswith("Extracted")


def test_extract_refused_when_tar_filter_unsupported(tmp_path, monkeypatch):
    # Filtre destegi olmayan Python (tarfile.data_filter yok): tar hic acilmaz.
    calls = []
    monkeypatch.delattr(tarfile, "data_filter")
    monkeypatch.setattr(file_processor.shutil, "unpack_archive", lambda *a, **kw: calls.append(a))
    arc = tmp_path / "a.tar"
    _evil_tar(arc)
    result = file_processor._process_archive(arc, "extract", {"destination": str(tmp_path / "out")})
    assert calls == [] and result.startswith("Extract refused")


def _zip(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("dir/hello.txt", "merhaba")


def test_valid_zip_extracts(tmp_path):
    arc = tmp_path / "ok.zip"
    _zip(arc)
    dest = tmp_path / "out"
    result = file_processor._process_archive(arc, "extract", {"destination": str(dest)})
    assert result.startswith("Extracted"), result
    assert (dest / "dir" / "hello.txt").read_text() == "merhaba"


def test_zip_extract_does_not_pass_tar_only_filter(tmp_path, monkeypatch):
    # filter yalnizca tar icindir; zip acicisi onu reddeden surumlerde zip
    # "Extract refused" ile acilamaz hale gelmemeli.
    real = shutil.unpack_archive

    def zip_unpacker_without_filter(filename, extract_dir=None, format=None, **kw):  # noqa: A002 - shutil imzasi
        if "filter" in kw:
            raise TypeError("unexpected keyword argument 'filter'")
        return real(filename, extract_dir, format)

    monkeypatch.setattr(file_processor.shutil, "unpack_archive", zip_unpacker_without_filter)
    arc = tmp_path / "ok.zip"
    _zip(arc)
    dest = tmp_path / "out"
    result = file_processor._process_archive(arc, "extract", {"destination": str(dest)})
    assert result.startswith("Extracted"), result
    assert (dest / "dir" / "hello.txt").is_file()
