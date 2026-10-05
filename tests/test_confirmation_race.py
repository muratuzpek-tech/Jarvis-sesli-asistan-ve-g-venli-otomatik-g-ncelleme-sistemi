"""tests/test_confirmation_race.py

Arayuzden gelen her metin komutu ayri bir thread'de _on_text_command'i
calistiriyor; bu fonksiyon bekleyen tehlikeli islemin onay durumunu kilitsiz
okuyup degistiriyordu. Asyncio thread'i ayni anda yeni bir islemi beklemeye
alirsa su siralama mumkundu:

    UI thread : bekleyen islem X var mi? -> evet
    loop      : yeni islem Y beklemeye alindi, onay=False
    UI thread : onay=True                -> kullanicinin X'e verdigi "evet" Y'yi onaylar

Kontrol-ve-degistir adimlari artik tek bir kilit altinda yapiliyor.
"""
import asyncio
import threading

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from tools.registry import registry

_calls: list[dict] = []


@registry.register(name="_pytest_race_destructive", description="t", parameters={},
                   security="destructive", category="test")
def _destructive(args, ctx):
    _calls.append(dict(args))
    return "RAN"


class _UI:
    def write_log(self, *_a, **_k):
        pass


class _Stop(BaseException):
    """_on_text_command'i onay blogundan sonra durdurmak icin."""


def _run(jl, args):
    return asyncio.run(jl._execute_registry_tool("_pytest_race_destructive", args))


class _InterleavingJarvis(JarvisLive):
    """UI thread'i bekleyen islemi OKUDUGU anda asyncio tarafinin yeni bir
    islemi beklemeye almasini zorlar - yarisin en kotu siralamasi."""

    @property
    def _pending_dangerous_action(self):
        hook = self.__dict__.get("_race_hook")
        if hook and threading.current_thread().name == "ui-cmd":
            self.__dict__["_race_hook"] = None
            hook()
        return self.__dict__.get("_pda")

    @_pending_dangerous_action.setter
    def _pending_dangerous_action(self, value):
        self.__dict__["_pda"] = value


@pytest.fixture
def jl(monkeypatch):
    _calls.clear()
    j = _InterleavingJarvis.__new__(_InterleavingJarvis)
    j.ui = _UI()
    j.session = None
    j._bg_tasks = None

    def _stop(*_a, **_k):
        raise _Stop()

    monkeypatch.setattr(main_mod, "log_turn", _stop)
    return j


def _user_types(j, text):
    """_on_text_command'i UI'daki gibi ayri bir thread'de calistirir."""
    errors = []

    def _target():
        j._loop, j.session = object(), object()
        try:
            j._on_text_command(text)
        except _Stop:
            pass
        except BaseException as e:  # pragma: no cover - teshis icin
            errors.append(e)

    t = threading.Thread(target=_target, name="ui-cmd")
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert not errors, errors


def test_yes_for_x_does_not_approve_y_queued_concurrently(jl):
    assert _run(jl, {"path": "X"}).startswith("CONFIRMATION_REQUIRED")

    loop_side = {}

    def _model_queues_y():
        # Asyncio tarafi: model baska argumanlarla yeni bir tehlikeli islem ister.
        t = threading.Thread(target=lambda: loop_side.setdefault("r", _run(jl, {"path": "Y"})))
        t.start()
        t.join(timeout=0.5)  # kilit varsa bekler, UI thread'i devam eder
        loop_side["t"] = t

    jl._race_hook = _model_queues_y
    _user_types(jl, "evet")
    loop_side["t"].join(timeout=5)
    assert loop_side["r"].startswith("CONFIRMATION_REQUIRED")

    # Kullanici Y'yi hic gormedi; onun "evet"i Y'yi calistirmamali.
    result = _run(jl, {"path": "Y"})

    assert result.startswith("CONFIRMATION_REQUIRED"), result
    assert _calls == []


def test_yes_still_approves_the_pending_action(jl):
    assert _run(jl, {"path": "X"}).startswith("CONFIRMATION_REQUIRED")
    _user_types(jl, "evet")
    assert _run(jl, {"path": "X"}).startswith("RAN")
    assert _calls == [{"path": "X"}]


def test_unrelated_text_clears_pending(jl):
    assert _run(jl, {"path": "X"}).startswith("CONFIRMATION_REQUIRED")
    _user_types(jl, "hava nasil")
    jl._grant_dangerous_confirmation()  # bekleyen islem yoksa onay hicbir seye baglanmaz
    assert _run(jl, {"path": "X"}).startswith("CONFIRMATION_REQUIRED")
    assert _calls == []

