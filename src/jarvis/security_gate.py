"""security_gate.py — tek güvenlik kapısı (docs/GUVENLIK_KAPISI_PLAN.md).

ADIM 0 + ADIM 1 (2026-10-05): veri tipleri, onay deposu, araç tanımları.
ADIM 2: authorize()/execute(). Sesli Gemini araç yolu (E1,
main.JarvisLive._execute_tool), registry yolu (E2, _execute_registry_tool) ve
ReAct iç döngüsü (E3, tools/agent/react_runtime) kapıdan geçer; agent_loop,
Brain Team ve yönlendiriciler henüz geçmez. Yürütülen (ALLOW ya da onaylı) ve
engellenen her çağrı audit()'e yazılır.
ADIM 3.0: MODEL_LIVE kaynağından yalnızca modelin gerçekten erişebildiği
araçlar kabul edilir (iç araçlar DENY, onay yuvasına dokunmaz).
ADIM 3.1: tek denetim kaydı JARVIS_HOME/memory/audit.log (core/audit_log).
ADIM 3.3: tek onay yuvası yerine MultiApprovalStore; modelin uydurduğu
discovered_* adları DENY (bekleyen onaya dokunmaz).

İçerik:

  * kapının veri tipleri (Source, Effect, Verdict, ResolvedCall, Decision),
  * onay deposu arayüzü (ApprovalStore), çok-istekli onay deposu
    (MultiApprovalStore, Adım 3.3: her istek kendi parmak izi, request_id,
    türü ve TTL'si ile; "evet" yalnızca en son duyurulana) ve eski tek-yuva
    arayüzünü bu depoya bağlayan ince sarmalayıcı (PendingSlotAdapter),
  * onay parmak izinin TEK kaynağı (fingerprint; main._action_fingerprint
    buraya devreder),
  * her araç için bir ToolSpec ve araçların HANGİ giriş yollarından
    erişilebildiğini mevcut tablolardan toplayan tarayıcı
    (collect_reachable_tools).

Tarayıcı kaynak dosyaları yalnızca AST ile OKUR: main.py, tools/ registry
kayıtları, executor_ai, Brain orkestratörü, CLI ajanı ve eklentiler
çalıştırılmaz/yüklenmez. Yalnızca tools_kopru.ALLOWED_TOOLS import edilerek
okunur (çalışma anında entegrasyonla eklenen araçlar da görünsün diye).

Yeni bir araç herhangi bir yola eklenip EFFECTS tablosuna girmezse
missing_specs() onu raporlar ve tests/test_security_gate.py kırmızı olur.
"""
from __future__ import annotations

import abc
import ast
import enum
import functools
import itertools
import os
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PKG_DIR = Path(__file__).resolve().parent            # src/jarvis
_PROJECT_ROOT = _PKG_DIR.parent.parent

MAIN_PY = _PKG_DIR / "main.py"
TOOLS_DIR = _PROJECT_ROOT / "tools"
EXECUTOR_PY = _PKG_DIR / "brains" / "executor_ai.py"
ORCHESTRATOR_PY = _PKG_DIR / "core" / "brain_orchestrator.py"
CLI_AGENT_PY = _PKG_DIR / "core" / "agent.py"
PLUGIN_DIR = _PKG_DIR / "plugins"                      # core/plugin_manager.PLUGIN_DIR


# ── Tipler ────────────────────────────────────────────────────────────────

class Source(enum.Enum):
    """Bir araç çağrısının geldiği giriş yolu (plan §1)."""
    MODEL_LIVE = "model_live"      # E1 Gemini Live araç çağrısı, E2 registry
    REACT = "react"                # E3 ReAct alt ajanı (registry.execute)
    ROUTER = "router"              # E4 deterministik metin yönlendiricileri
    AGENT_LOOP = "agent_loop"      # E5 arka plan görev döngüsü (tools_kopru)
    BRAIN_TEAM = "brain_team"      # E6 Brain Team orkestratörü
    SCHEDULER = "scheduler"        # E13 zamanlanmış istem
    PROACTIVE = "proactive"        # proaktif mod / brifing
    CLI_AGENT = "cli_agent"        # E15 CLI metin ajanı (core/agent.py)
    PLUGIN = "plugin"              # E16 CLI eklentileri


class Effect(enum.IntEnum):
    """Aracın en kötü etkisi; büyük = tehlikeli."""
    READ = 0
    MUTATE = 1
    EXTERNAL = 2
    EXECUTE = 3
    SYSTEM = 4


class Verdict(enum.Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


# Onaylanan işlemin kimliği: araç adı + argümanlar. main.JarvisLive.
# _action_fingerprint ve denetim kaydı bunu kullanır - TEK uygulama
# jarvis.core.call_fingerprint'te (audit_log kapıyı import etmesin diye).
from jarvis.core.call_fingerprint import fingerprint  # noqa: E402


@dataclass(frozen=True)
class ResolvedCall:
    """Bir kez çözülmüş çağrı. Kapı devreye girdiğinde yürütme bu nesneyle
    yapılacak, yeniden çözümleme olmayacak (plan §4.1-1)."""
    tool: str
    action: str | None
    params: dict
    targets: tuple[str, ...]
    effect: Effect
    fingerprint: str
    source: "Source | None" = None

    @classmethod
    def for_pending(cls, action: str, args: dict) -> "ResolvedCall":
        """Brain Team / agent_loop gibi sözde eylemler için (onay yuvası
        bunları 'brain_team' / 'agent_loop' adıyla tutar)."""
        return cls(tool=action, action=None, params=dict(args), targets=(),
                   effect=Effect.EXECUTE, fingerprint=fingerprint(action, args))


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    call: ResolvedCall | None
    reason: str
    model_message: str
    user_prompt: str | None = None
    request_id: str | None = None   # iç kimlik; modele verilmez
    # ALLOW kararında aracın KENDİ önizleme/onay kodu protokolü varsa
    # (TOOL_CODE / TERMINAL_CODE): kod araçtan alınır, modele gösterilmez,
    # yalnızca gerçek kullanıcı onayından sonra araca geri verilir.
    protocol: str | None = None


@dataclass(frozen=True)
class Grant:
    """Gerçek kullanıcı turundan gelen, TEK bir çağrıya (parmak izi) bağlı
    onay. code: aracın kendi önizleme kodu (TOOL_CODE/TERMINAL_CODE)."""
    fingerprint: str
    code: str | None = None


TOOL_CODE = "tool_code"          # "ONAY GEREKLİ ... confirm_code='X'" (file_controller, code_helper, self_improve)
TERMINAL_CODE = "terminal_code"  # "ONAY GEREKLİ ...\nOnay kodu: X" (terminal_tool)


# ── Onay deposu ───────────────────────────────────────────────────────────

class ApprovalStore(abc.ABC):
    """Onay deposu arayüzü. Onay yalnızca girdi katmanının bildirdiği gerçek
    kullanıcı turundan gelir; model argümanı hiçbir zaman onay değildir."""

    @abc.abstractmethod
    def request(self, call: ResolvedCall) -> str:
        """Çağrıyı onay bekleyen olarak kaydeder; iç istek kimliğini döner."""

    @abc.abstractmethod
    def grant_from_user_turn(self) -> None:
        """Kullanıcının gerçek turundaki "evet"i bekleyen isteğe uygular."""

    @abc.abstractmethod
    def consume(self, call: ResolvedCall) -> bool:
        """Aynı çağrı için verilmiş, süresi dolmamış onay varsa tüketir."""

    @abc.abstractmethod
    def cancel(self, request_id: str | None = None) -> None:
        """Bekleyen isteği iptal eder (request_id verilirse yalnızca o)."""

    @abc.abstractmethod
    def pending(self) -> tuple[str, str] | None:
        """(araç, parmak izi) ya da None."""

    @abc.abstractmethod
    def take_grant(self, decision: "Decision") -> Grant | None:
        """Karar için verilmiş gerçek kullanıcı onayını tüketip Grant döner."""

    @abc.abstractmethod
    def remember_code(self, call: ResolvedCall, code: str) -> None:
        """Aracın önizleme kodunu saklar ve çağrıyı onay bekleyen yapar."""


# ── Çok-istekli onay deposu (Adım 3.3) ────────────────────────────────────
#
# Kullanıcı kararları (2026-10-06): sesli araç yolu onayı 60 sn, arka plan
# (Brain Team / agent_loop) isteği 10 dk geçerli; "evet" yalnızca EN SON
# duyurulan isteği onaylar, "hayır" yalnızca onu iptal eder; diğerleri
# bekler ve sırayla yeniden sorulur.

FOREGROUND_TTL_S = 60
BACKGROUND_TTL_S = 600

KIND_VOICE = "voice"              # sesli araç yolu (E1/E2) ve uyumluluk kayıtları
KIND_AGENT_LOOP = "agent_loop"    # arka plan görev döngüsünün onay bekleyen adımı
KIND_BRAIN_TEAM = "brain_team"    # Brain Team orkestratörünün onay bekleyen adımı

_BACKGROUND_KINDS = {Source.AGENT_LOOP: KIND_AGENT_LOOP, Source.BRAIN_TEAM: KIND_BRAIN_TEAM}


def _kind_of(call: ResolvedCall) -> str:
    """İsteğin türü çağrının KAYNAĞINDAN gelir, araç adından değil: modelin
    agent_loop(retry) çağrısı (MODEL_LIVE) sesli istektir; arka plan döngüsünün
    adımı (ResolvedCall.for_pending, kaynak yok) agent_loop isteğidir."""
    if call.source is not None:
        return _BACKGROUND_KINDS.get(call.source, KIND_VOICE)
    return {"agent_loop": KIND_AGENT_LOOP, "brain_team": KIND_BRAIN_TEAM}.get(call.tool, KIND_VOICE)


@dataclass
class ApprovalRequest:
    """Depodaki tek onay isteği. request_id iç kimliktir; modele verilmez."""
    request_id: str
    call: ResolvedCall
    kind: str
    background: bool
    task_id: str | None
    asked_at: float                 # son duyurulma anı (arka planda: ilk duyuru)
    seq: int                        # duyuru sırası; en büyüğü "en son duyurulan"
    message: str | None = None      # yeniden sorulurken kullanıcıya okunacak metin
    code: str | None = None         # aracın kendi önizleme kodu (modele gitmez)
    granted_at: float | None = None

    @property
    def tool(self) -> str:
        return self.call.tool

    @property
    def fingerprint(self) -> str:
        return self.call.fingerprint

    @property
    def ttl(self) -> float:
        return BACKGROUND_TTL_S if self.background else FOREGROUND_TTL_S


class MultiApprovalStore(ApprovalStore):
    """Her isteği kendi parmak izi, request_id, türü ve TTL'si ile tutar.

    * request(): istek EN SON duyurulan olur; var olan istekler silinmez.
      Aynı tür + aynı parmak izi yeniden istenirse eskisi düşer (verilmiş
      onay yeni isteğe taşınmaz).
    * answer(True/False): yalnızca en son duyurulan isteğe uygulanır.
    * consume(): parmak izi eşleşen, kullanıcı onayı FOREGROUND_TTL_S
      içinde verilmiş isteği tüketir (tek kullanım).
    * Süresi dolan istekler görünmez: sesli istek son duyurusundan
      FOREGROUND_TTL_S, arka plan isteği ilk duyurusundan BACKGROUND_TTL_S
      sonra düşer. expire() düşen arka plan isteklerini (bildirim için) döner.
    * "hayır" ile iptal edilen parmak izi, kullanıcının bir sonraki (ret
      olmayan) turuna kadar reddedilmiş sayılır (is_denied); model aynı
      çağrıyı hemen yeniden isteyip kuyruğun başına geçemez.

    Onay yalnızca girdi katmanının bildirdiği gerçek kullanıcı turundan gelir;
    bu sınıf kod üretmez ve modele hiçbir şey döndürmez.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None, lock=None,
                 on_drop: Callable[[ApprovalRequest], None] | None = None) -> None:
        # time.monotonic çağrı anında okunur (testler onu yamayabilir).
        self._clock = clock or (lambda: time.monotonic())
        self._lock = lock if lock is not None else threading.RLock()
        self._on_drop = on_drop
        self._items: dict[str, ApprovalRequest] = {}
        self._seq = itertools.count(1)
        self._denied: set[str] = set()

    # -- iç yardımcılar (kilit altında çağrılır) --

    def _visible(self, rec: ApprovalRequest, now: float) -> bool:
        if rec.granted_at is not None and now - rec.granted_at <= FOREGROUND_TTL_S:
            return True
        return now - rec.asked_at <= rec.ttl

    def _ordered(self, kind: str | None = None) -> list[ApprovalRequest]:
        now = self._clock()
        return sorted((r for r in self._items.values()
                       if self._visible(r, now) and (kind is None or r.kind == kind)),
                      key=lambda r: r.seq)

    def _drop(self, rid: str) -> ApprovalRequest | None:
        rec = self._items.pop(rid, None)
        if rec is not None and self._on_drop is not None:
            try:
                self._on_drop(rec)
            except Exception:
                pass
        return rec

    def _purge_foreground(self) -> None:
        now = self._clock()
        for rid in [r.request_id for r in self._items.values()
                    if not r.background and not self._visible(r, now)]:
            self._drop(rid)

    # -- istek --

    def request(self, call: ResolvedCall, *, background: bool | None = None,
                task_id: str | None = None, kind: str | None = None,
                message: str | None = None, code: str | None = None) -> str:
        kind = kind or _kind_of(call)
        if background is None:
            background = kind != KIND_VOICE
        with self._lock:
            self._purge_foreground()
            for rec in list(self._items.values()):
                if rec.kind == kind and rec.fingerprint == call.fingerprint:
                    self._drop(rec.request_id)
            rid = uuid.uuid4().hex[:12]
            self._items[rid] = ApprovalRequest(
                request_id=rid, call=call, kind=kind, background=bool(background),
                task_id=task_id, asked_at=self._clock(), seq=next(self._seq),
                message=message, code=code)
            return rid

    def get(self, request_id: str | None) -> ApprovalRequest | None:
        with self._lock:
            rec = self._items.get(request_id) if request_id else None
            return rec if rec is not None and self._visible(rec, self._clock()) else None

    def records(self, kind: str | None = None) -> list[ApprovalRequest]:
        """Görünür istekler, duyuru sırasıyla (en son duyurulan sonda)."""
        with self._lock:
            return self._ordered(kind)

    def latest_record(self) -> ApprovalRequest | None:
        with self._lock:
            ordered = self._ordered()
            return ordered[-1] if ordered else None

    def latest(self) -> str | None:
        rec = self.latest_record()
        return rec.request_id if rec else None

    def pending_ids(self) -> list[str]:
        return [r.request_id for r in self.records()]

    def reannounce(self) -> ApprovalRequest | None:
        """Sırada bekleyen en son isteği yeniden duyurulmuş sayar (sesli
        isteğin süresi yenilenir; arka plan isteğininki ilk duyurudan işler)."""
        with self._lock:
            rec = self.latest_record()
            if rec is not None:
                rec.seq = next(self._seq)
                if not rec.background:
                    rec.asked_at = self._clock()
            return rec

    # -- kullanıcı cevabı --

    def grant(self, request_id: str) -> bool:
        with self._lock:
            rec = self.get(request_id)
            if rec is None:
                return False
            rec.granted_at = self._clock()
            return True

    def revoke(self, request_id: str) -> None:
        with self._lock:
            rec = self._items.get(request_id)
            if rec is not None:
                rec.granted_at = None

    def deny(self, request_id: str) -> ApprovalRequest | None:
        """İsteği kullanıcının reddi ile düşürür; parmak izi bir sonraki
        kullanıcı turuna kadar reddedilmiş sayılır."""
        with self._lock:
            rec = self._drop(request_id)
            if rec is not None:
                self._denied.add(rec.fingerprint)
            return rec

    def answer(self, confirmed: bool) -> str | None:
        with self._lock:
            rid = self.latest()
            if rid is None:
                return None
            if confirmed:
                self.grant(rid)
            else:
                self.deny(rid)
            return rid

    def is_denied(self, call: ResolvedCall) -> bool:
        with self._lock:
            return call.fingerprint in self._denied

    def clear_denials(self) -> None:
        with self._lock:
            self._denied.clear()

    def consume_record(self, call: ResolvedCall) -> ApprovalRequest | None:
        with self._lock:
            now = self._clock()
            for rec in reversed(self._ordered()):
                if (rec.tool == call.tool and rec.fingerprint == call.fingerprint
                        and rec.granted_at is not None
                        and now - rec.granted_at <= FOREGROUND_TTL_S):
                    return self._items.pop(rec.request_id)
            return None

    def drop_kind(self, kind: str) -> None:
        with self._lock:
            for rid in [r.request_id for r in self._items.values() if r.kind == kind]:
                self._drop(rid)

    def expire(self) -> list[ApprovalRequest]:
        """Süresi dolan istekleri siler; arka plan olanları döner."""
        with self._lock:
            now = self._clock()
            gone = [r for r in self._items.values() if not self._visible(r, now)]
            for rec in gone:
                self._drop(rec.request_id)
            return sorted((r for r in gone if r.background), key=lambda r: r.seq)

    # -- ApprovalStore arayüzü --

    def grant_from_user_turn(self) -> None:
        self.answer(True)

    def consume(self, call: ResolvedCall) -> bool:
        return self.consume_record(call) is not None

    def cancel(self, request_id: str | None = None) -> None:
        """request_id (ya da parmak izi) verilirse yalnızca o; yoksa hepsi."""
        with self._lock:
            for rec in list(self._items.values()):
                if request_id is None or request_id in (rec.request_id, rec.fingerprint):
                    self._drop(rec.request_id)
            if request_id is None:
                self._denied.clear()

    def pending(self) -> tuple[str, str] | None:
        rec = self.latest_record()
        return (rec.tool, rec.fingerprint) if rec else None

    def take_grant(self, decision: "Decision") -> Grant | None:
        call = decision.call
        if call is None or decision.verdict is Verdict.DENY:
            return None
        if decision.verdict is not Verdict.NEEDS_APPROVAL and \
                decision.protocol not in (TOOL_CODE, TERMINAL_CODE):
            return None
        rec = self.consume_record(call)
        if rec is None:
            return None
        return Grant(call.fingerprint, rec.code)

    def remember_code(self, call: ResolvedCall, code: str) -> None:
        """Aracın önizleme kodu yalnızca en yeni önizlemede saklanır; önceki
        önizleme (terminal kaydı dahil) düşer."""
        with self._lock:
            for rid in [r.request_id for r in self._items.values() if r.code]:
                self._drop(rid)
            self.request(call, code=code)


def rejected_message(call: ResolvedCall) -> str:
    """Kullanıcının az önce "hayır" dediği çağrı yeniden istendiğinde modele
    giden metin (sır ya da kod içermez)."""
    return (f"REDDEDILDI:{call.tool}: Kullanici bu islemi az once reddetti. Araci tekrar "
            "cagirma ve onay isteme; kullanici yeniden acikca isterse o zaman cagir.")


def rejected_by_user(decision: "Decision", grant: Grant | None,
                     store: "MultiApprovalStore") -> str | None:
    """Onay isteyecek (NEEDS_APPROVAL ya da önizleme kodlu) bir çağrı
    kullanıcının az önce reddettiği çağrıysa ret metni; değilse None. Model
    reddedilen çağrıyı hemen yeniden isteyip kuyruğun başına geçemez."""
    call = decision.call
    if grant is not None or call is None:
        return None
    if decision.verdict is not Verdict.NEEDS_APPROVAL and decision.protocol is None:
        return None
    return rejected_message(call) if store.is_denied(call) else None


class PendingSlotAdapter(ApprovalStore):
    """Eski adı korunan ince sarmalayıcı: JarvisLive'ın çok-istekli onay
    deposunu (owner._approvals, MultiApprovalStore) eski tek-yuva arayüzüyle
    sunar. Kural eklemez; request() eski sözleşmedeki gibi parmak izini döner."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    @property
    def _store(self) -> MultiApprovalStore:
        return self._owner._approvals

    def request(self, call: ResolvedCall) -> str:
        self._store.request(call)
        return call.fingerprint

    def grant_from_user_turn(self) -> None:
        self._store.grant_from_user_turn()

    def consume(self, call: ResolvedCall) -> bool:
        return self._store.consume(call)

    def cancel(self, request_id: str | None = None) -> None:
        self._store.cancel(request_id)

    def pending(self) -> tuple[str, str] | None:
        return self._store.pending()

    def take_grant(self, decision: "Decision") -> Grant | None:
        return self._store.take_grant(decision)

    def remember_code(self, call: ResolvedCall, code: str) -> None:
        self._store.remember_code(call, code)


# ── Araç tanımları (plan §3) ──────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: Effect                                   # en kötü durum
    sources: frozenset[Source] = frozenset()         # erişilebildiği yollar
    effect_of: Callable[[Mapping], Effect] | None = field(default=None, compare=False)

    def effect_for(self, params: Mapping | None) -> Effect:
        return self.effect_of(params or {}) if self.effect_of else self.effect


# Eyleme bağlı etkiler - MEVCUT sınıflandırıcıları kullanır, kopyalamaz.

def _file_controller_effect(params: Mapping) -> Effect:
    from jarvis.actions.file_controller import is_readonly_action
    return Effect.READ if is_readonly_action(params.get("action")) else Effect.MUTATE


def _terminal_effect(params: Mapping) -> Effect:
    # Fail-closed sapma: terminal_tool "cd X && cmd" önekini ayıklayıp cmd'yi
    # değerlendirir; burada önek ayıklanmaz, böyle komutlar EXECUTE sayılır.
    from jarvis.actions.terminal_tool import _is_readonly
    try:
        argv = shlex.split(str(params.get("command", "")), posix=os.name != "nt")
    except ValueError:
        return Effect.EXECUTE
    return Effect.READ if _is_readonly(argv) else Effect.EXECUTE


def _computer_settings_effect(params: Mapping) -> Effect:
    from jarvis.actions.computer_settings import _DANGEROUS_ACTIONS
    action = str(params.get("action", "")).lower().strip().replace(" ", "_").replace("-", "_")
    if not action or action in _DANGEROUS_ACTIONS:
        # Boş eylemde computer_settings eylemi LLM ile tahmin eder (kapatma
        # dahil) - en kötü durum.
        return Effect.SYSTEM
    return Effect.MUTATE


def _game_updater_effect(params: Mapping) -> Effect:
    shutdown = str(params.get("shutdown_when_done", "false")).lower() == "true"
    return Effect.SYSTEM if shutdown else Effect.EXECUTE


def _save_flag(base: Effect) -> Callable[[Mapping], Effect]:
    def _effect(params: Mapping) -> Effect:
        saving = str(params.get("save", "false")).lower() in ("true", "1", "yes")
        return max(base, Effect.MUTATE) if saving else base
    return _effect


_DESKTOP_READ_ACTIONS = frozenset({"list", "stats", "current_wallpaper"})
_BROWSER_READ_ACTIONS = frozenset({"get_text", "get_url", "list_browsers"})


def _action_effect(read_actions: frozenset, otherwise: Effect) -> Callable[[Mapping], Effect]:
    def _effect(params: Mapping) -> Effect:
        action = str(params.get("action", "")).lower().strip()
        return Effect.READ if action in read_actions else otherwise
    return _effect


def _coder_ai_effect(params: Mapping) -> Effect:
    return Effect.READ if params.get("operation") == "analyze" else Effect.MUTATE


def _const(effect: Effect) -> Callable[[Mapping], Effect]:
    return lambda _params: effect


# (en kötü etki, eyleme bağlı etki). Plan §3 tablosu.
EFFECTS: dict[str, tuple[Effect, Callable[[Mapping], Effect] | None]] = {
    # 3.1 Gemini araçları (main.py TOOL_DECLARATIONS / _execute_tool)
    "open_app": (Effect.EXECUTE, None),
    "web_search": (Effect.READ, None),
    "terminal": (Effect.EXECUTE, _terminal_effect),
    "system_status": (Effect.READ, None),
    "system_scan_and_repair": (Effect.EXECUTE, None),
    "weather_report": (Effect.READ, None),
    "send_message": (Effect.EXTERNAL, None),
    "reminder": (Effect.MUTATE, None),
    "youtube_video": (Effect.EXTERNAL, None),
    "screen_process": (Effect.READ, None),
    "close_camera": (Effect.MUTATE, None),
    "computer_settings": (Effect.SYSTEM, _computer_settings_effect),
    "browser_control": (Effect.EXTERNAL, _action_effect(_BROWSER_READ_ACTIONS, Effect.EXTERNAL)),
    "file_controller": (Effect.MUTATE, _file_controller_effect),
    "task_manager": (Effect.MUTATE, None),
    "health_check": (Effect.READ, None),
    "start_parallel_task": (Effect.EXECUTE, None),
    "check_agent_board": (Effect.READ, None),
    "desktop_control": (Effect.MUTATE, _action_effect(_DESKTOP_READ_ACTIONS, Effect.MUTATE)),
    "code_helper": (Effect.EXECUTE, None),
    "code_search": (Effect.READ, None),
    "dev_agent": (Effect.EXECUTE, None),
    "self_improve": (Effect.MUTATE, None),
    "agent_loop": (Effect.MUTATE, None),
    "recall_conversation": (Effect.READ, None),
    "github_arama": (Effect.READ, None),
    "discovered_topydo": (Effect.EXECUTE, None),
    "discovered_jc": (Effect.EXECUTE, None),
    "computer_control": (Effect.EXECUTE, None),
    "game_updater": (Effect.SYSTEM, _game_updater_effect),
    "flight_finder": (Effect.MUTATE, _save_flag(Effect.READ)),
    "shutdown_jarvis": (Effect.SYSTEM, None),
    "file_processor": (Effect.MUTATE, None),
    "save_memory": (Effect.MUTATE, None),
    # 3.2 Registry (tools/)
    "agentic_code": (Effect.EXECUTE, None),
    "react_agent": (Effect.EXECUTE, None),     # kayıtlı her registry aracını çağırabilir
    "spotify_control": (Effect.EXTERNAL, None),
    # 3.3 agent_loop köprüsü (tools_kopru.ALLOWED_TOOLS) - ek olanlar
    "windows_system": (Effect.READ, None),
    "github_arac_bul_ve_degerlendir": (Effect.MUTATE, None),
    "entegrasyon_uygula": (Effect.EXECUTE, None),
    "discovery_register": (Effect.MUTATE, None),
    # 3.4 Brain Team uygulayıcıları
    "backup_create": (Effect.MUTATE, None),
    "backup_rollback": (Effect.MUTATE, None),
    "vault_encrypt": (Effect.MUTATE, None),
    "vault_decrypt": (Effect.MUTATE, None),
    "github_search": (Effect.READ, None),
    "coder_ai": (Effect.MUTATE, _coder_ai_effect),
    "research_ai": (Effect.READ, None),
    # 3.5 CLI ajanı (core/agent.py TOOLS)
    "file_list": (Effect.READ, None),
    "file_read": (Effect.READ, None),
    "file_write": (Effect.MUTATE, None),
    "run_python": (Effect.EXECUTE, None),
    "remember": (Effect.MUTATE, None),
    "file_delete": (Effect.MUTATE, None),
    # Deterministik yönlendirici sözde aracı (brain_team_tool / Brain görevi)
    "brain_team": (Effect.MUTATE, None),
}

# Entegrasyonla eklenen dış kod: her ad için fail-closed genel spec.
DISCOVERED_PREFIX = "discovered_"

# Bir yolun bugün onaysız çalıştırdığı ama READ olmayan, BİLİNÇLİ istisnalar.
KNOWN_UNGATED_NON_READ: dict[str, str] = {
    "github_arac_bul_ve_degerlendir": (
        "karantinaya indirir ve LLM'e analiz ettirir; kodu çalıştırmaz. "
        "tools_kopru._READONLY_TOOLS'ta bilinçli olarak onaysız (plan §3.3)."
    ),
}


# ── Erişim yollarını toplayan tarayıcı (yalnızca okur) ────────────────────

def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _dict_name_values(node: ast.AST) -> list[str]:
    """Bir liste/ifade içindeki {"name": "..."} sözlüklerinin adları."""
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if (isinstance(k, ast.Constant) and k.value == "name"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    names.append(v.value)
    return names


def _dict_keys(node: ast.AST | None) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def _module_assign(tree: ast.Module, name: str) -> ast.AST | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            return stmt.value
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                and stmt.target.id == name:
            return stmt.value
    return None


def _find_function(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _compared_constants(func: ast.AST | None, var: str) -> list[str]:
    """func içinde `var == "x"` karşılaştırmalarındaki "x" değerleri."""
    out = []
    if func is None:
        return out
    for node in ast.walk(func):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == var and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)):
            out.append(node.comparators[0].value)
    return out


# _on_text_command'daki deterministik yönlendiricilerin çağırdığı fonksiyon
# -> araç eşlemesi (main.py, plan §1.2 E4).
_ROUTER_CALLS = {
    "terminal_tool": "terminal",
    "brain_team_tool": "brain_team",
    "agent_loop_tool": "agent_loop",
    "get_file_info": "file_controller",
}


def _router_tools(func: ast.AST | None) -> list[str]:
    out = []
    if func is None:
        return out
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname in _ROUTER_CALLS:
                out.append(_ROUTER_CALLS[fname])
            # FILE_MODIFICATION / FILE_ANALYSIS: orch.tasks.create(...) -> Brain görevi
            if (fname == "create" and isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Attribute) and fn.value.attr == "tasks"):
                out.append("brain_team")
        # ALLOWED_TOOLS["windows_system"](...)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "ALLOWED_TOOLS" and isinstance(node.slice, ast.Constant)):
            out.append(node.slice.value)
    return out


def _registry_names() -> list[str]:
    names = []
    for path in sorted(TOOLS_DIR.rglob("*.py")) if TOOLS_DIR.is_dir() else []:
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if fname != "register":
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    names.append(kw.value.value)
    return names


def collect_reachable_tools() -> dict[str, set[Source]]:
    """Araç adı -> erişilebildiği giriş yolları. Kaynaklar (plan §1-§3):
    main.py TOOL_DECLARATIONS ve _execute_tool dalları (MODEL_LIVE),
    _on_text_command yönlendiricileri (ROUTER), tools/ registry kayıtları
    (MODEL_LIVE + REACT), tools_kopru.ALLOWED_TOOLS (AGENT_LOOP),
    executor_ai._ALLOWED_ACTIONS ve _execute_step ajanları (BRAIN_TEAM),
    core/agent.py TOOLS (CLI_AGENT), eklenti dosyaları (PLUGIN)."""
    reach: dict[str, set[Source]] = {}

    def add(names, source: Source) -> None:
        for n in names:
            reach.setdefault(n, set()).add(source)

    main_tree = _parse(MAIN_PY)
    if main_tree is not None:
        decls = _module_assign(main_tree, "TOOL_DECLARATIONS")
        add(_dict_name_values(decls) if decls is not None else [], Source.MODEL_LIVE)
        add(_compared_constants(_find_function(main_tree, "_execute_tool"), "name"), Source.MODEL_LIVE)
        add(_router_tools(_find_function(main_tree, "_on_text_command")), Source.ROUTER)

    registry = _registry_names()
    add(registry, Source.MODEL_LIVE)
    add(registry, Source.REACT)

    from jarvis.actions.tools_kopru import ALLOWED_TOOLS
    add(list(ALLOWED_TOOLS), Source.AGENT_LOOP)

    exec_tree = _parse(EXECUTOR_PY)
    if exec_tree is not None:
        add(_dict_keys(_module_assign(exec_tree, "_ALLOWED_ACTIONS")), Source.BRAIN_TEAM)
    orch_tree = _parse(ORCHESTRATOR_PY)
    if orch_tree is not None:
        agents = _compared_constants(_find_function(orch_tree, "_execute_step"), "agent")
        add([a for a in agents if a != "executor_ai"], Source.BRAIN_TEAM)

    cli_tree = _parse(CLI_AGENT_PY)
    if cli_tree is not None:
        add(_dict_keys(_module_assign(cli_tree, "TOOLS")), Source.CLI_AGENT)

    if PLUGIN_DIR.is_dir():
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            tree = _parse(path)
            if tree is not None:
                add(_dict_name_values(tree), Source.PLUGIN)

    return reach


def _known(name: str) -> bool:
    return name in EFFECTS or name.startswith(DISCOVERED_PREFIX)


def missing_specs(reachable: dict[str, set[Source]] | None = None) -> dict[str, set[Source]]:
    """Erişilebilir ama spec'i olmayan araçlar (boş olmalı)."""
    reachable = collect_reachable_tools() if reachable is None else reachable
    return {name: srcs for name, srcs in reachable.items() if not _known(name)}


def _registry_entry(name: str):
    """tools/ registry kaydı (çalışma anında kaydedilenler dahil) ya da None."""
    try:
        from tools.registry import registry
    except Exception:
        return None
    return registry.get(name)


# tools/security.SecurityLevel (READ_ONLY=0, NORMAL=1, DANGEROUS=2,
# DESTRUCTIVE=3) -> etki. Kaydın kendi seviye beyanı spec'in kaynağıdır
# (plan §4.3: "registry kaydı spec üretir").
_REGISTRY_LEVEL_EFFECT = {0: Effect.READ, 1: Effect.MUTATE, 2: Effect.EXECUTE, 3: Effect.EXECUTE}


def _make_spec(name: str, sources) -> ToolSpec | None:
    if name in EFFECTS:
        effect, effect_of = EFFECTS[name]
    elif name.startswith(DISCOVERED_PREFIX):
        effect, effect_of = Effect.EXECUTE, None
    elif (entry := _registry_entry(name)) is not None:
        effect, effect_of = _REGISTRY_LEVEL_EFFECT.get(int(entry.security), Effect.SYSTEM), None
    else:
        return None
    return ToolSpec(name=name, effect=effect, sources=frozenset(sources), effect_of=effect_of)


def build_specs(reachable: dict[str, set[Source]] | None = None) -> dict[str, ToolSpec]:
    reachable = collect_reachable_tools() if reachable is None else reachable
    specs = {}
    for name, srcs in reachable.items():
        spec = _make_spec(name, srcs)
        if spec is not None:
            specs[name] = spec
    return specs


def spec_for(name: str) -> ToolSpec | None:
    """Tek aracın spec'i (erişim yolları hesaplanmadan). Bilinmeyen -> None."""
    return _make_spec(name, ())


# Kendi onay kodunu MODELDEN bekleyen eski akışlar (plan bulgusu B, Adım 6'da
# kapıya taşınacak). Bunlarda modelin confirm_code'u araca aynen iletilir;
# kodu doğrulayan araçtır (dev_agent.confirmation_problem).
_MODEL_CODE_LEGACY = frozenset({"start_parallel_task", "dev_agent"})


def _targets(tool: str, params: Mapping) -> tuple[str, ...]:
    if tool == "file_controller":
        try:
            from jarvis.actions.file_controller import _resolve_path
            base = _resolve_path(str(params.get("path") or "desktop"))
            name = str(params.get("name") or "")
            out = [str((base / name if name else base).resolve())]
            if params.get("destination"):
                out.append(str(_resolve_path(str(params["destination"])).resolve()))
            return tuple(out)
        except Exception:
            return ()
    for key in ("receiver", "url", "file_path", "path", "app_name", "command"):
        if params.get(key):
            return (str(params[key]),)
    return ()


def resolve(tool: str, args: Mapping | None, source: Source) -> ResolvedCall:
    """Çağrıyı bir kez çözer. Modelin gönderdiği confirm_code çağrının
    parçası değildir (yalnızca _MODEL_CODE_LEGACY araçları hariç). Bilinmeyen
    araç en kötü etkiyle (SYSTEM) döner - fail-closed."""
    params = {k: v for k, v in dict(args or {}).items()
              if k != "confirm_code" or tool in _MODEL_CODE_LEGACY}
    spec = spec_for(tool)
    effect = spec.effect_for(params) if spec else Effect.SYSTEM
    action = params.get("action")
    if tool == "file_controller":
        from jarvis.actions.file_controller import normalize_action
        action = normalize_action(action)
    return ResolvedCall(tool=tool, action=str(action) if action not in (None, "") else None,
                        params=params, targets=_targets(tool, params), effect=effect,
                        fingerprint=fingerprint(tool, params), source=source)


# ── Karar: authorize ──────────────────────────────────────────────────────

ALLOW, APPROVE = "allow", "approve"


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "evet")


def _file_controller_live(params: Mapping) -> str:
    from jarvis.actions.file_controller import is_readonly_action, normalize_action
    action = normalize_action(params.get("action"))
    if is_readonly_action(action):
        return ALLOW
    if action in ("move", "delete_all_files"):
        return TOOL_CODE          # araç kendi önizlemesini ve kodunu üretir
    return APPROVE


def _save_memory_live(params: Mapping) -> str:
    from jarvis.memory.sanitizer import instruction_markers
    text = " ".join(str(params.get(k, "")) for k in ("category", "key", "value"))
    return APPROVE if instruction_markers(text) else ALLOW


# Sesli yol (MODEL_LIVE) politikası. Tabloda olmayan araç: READ -> ALLOW,
# geri kalan her şey -> APPROVE (fail-closed). Tablodaki ALLOW'lar BUGÜNKÜ
# davranışı korur ve gerekçesi yazılıdır.
_LIVE_POLICY: dict[str, tuple[Callable[[Mapping], str], str]] = {
    "terminal": (lambda p: TERMINAL_CODE, "salt-okunur komut hemen; diğerleri terminal_tool önizlemesi + kod"),
    "self_improve": (lambda p: TOOL_CODE, "self_improve önizlemesi + kod"),
    "code_helper": (lambda p: TOOL_CODE, "registry varsa agentic_code (kendi onayı); yoksa üzerine yazma önizlemesi"),
    "file_controller": (_file_controller_live, "okuma serbest; taşıma/toplu silme önizleme+kod; diğer değişiklikler onay"),
    "save_memory": (_save_memory_live, "yalnızca talimata benzeyen içerik onay ister"),
    "youtube_video": (lambda p: APPROVE if _truthy(p.get("save", False)) else ALLOW,
                      "save=true dosya yazar; oynatma/özet bugünkü gibi serbest"),
    # Kendi onay akışı olanlar (main.py dalı onay deposundan tüketir):
    "computer_settings": (lambda p: ALLOW, "restart/shutdown/lock kendi onay akışında"),
    "game_updater": (lambda p: ALLOW, "shutdown_when_done kendi onay akışında"),
    "shutdown_jarvis": (lambda p: ALLOW, "kendi onay akışında"),
    "start_parallel_task": (lambda p: ALLOW, "dev_agent kod akışı (bulgu B, Adım 6)"),
    "dev_agent": (lambda p: ALLOW, "registry'de agentic_code (kendi onayı); yoksa dev_agent kod akışı"),
    # Bugün onaysız; plan Adım 7'de gözden geçirilecek:
    "close_camera": (lambda p: ALLOW, "kamerayı kapatır; geri alınabilir"),
    "task_manager": (lambda p: ALLOW, "ertelenmiş istem kaydı (bulgu E, Adım 7)"),
    # retry: kullanicinin iptal ettigi/basarisiz gorevi yeniden kuyruga alir -
    # model bunu kendi basina yapamaz, ayni task_id icin kullanici onayi gerekir.
    "agent_loop": (lambda p: APPROVE if str(p.get("action", "")).lower().strip() == "retry" else ALLOW,
                   "görev ekler/listeler/iptal eder; retry kullanıcı onayı ister"),
    "flight_finder": (lambda p: ALLOW, "arama; save bugünkü gibi serbest"),
}


def _approval_texts(call: ResolvedCall) -> tuple[str, str]:
    from jarvis.core.audit_log import format_params
    danger = "TEHLIKELI" if call.effect >= Effect.EXTERNAL else "DIKKAT"
    # Gizli argumanlar "***", icerik alanlari yalnizca uzunluk (kesme tek
    # basina kisa bir parolayi gizlemez).
    ps = format_params(call.params, limit=40)
    model_message = (
        f"CONFIRMATION_REQUIRED:{call.tool}:{danger} arac cagrisi: {ps}. Kullaniciya "
        f"ne yapilacagini TEK cumleyle anlat ve 'evet' ya da 'hayir' demesini iste. "
        f"Kullanici bir sonraki mesajinda acikca onaylarsa araci AYNI parametrelerle "
        f"BIR KEZ tekrar cagir; onay kodu yoktur, onay gelmeden cagirma."
    )
    target = ", ".join(call.targets) or "—"
    user_prompt = (f"Onay gerekiyor: '{call.tool}' aracı çalıştırılacak. "
                   f"Argümanlar: {ps or '—'}. Hedef: {target}.")
    return model_message, user_prompt


def _registry_policy(tool: str) -> tuple[str, str] | None:
    """Registry aracının onay politikası kaydın kendi seviye beyanından:
    READ_ONLY/NORMAL onaysız (bugünkü davranış), DANGEROUS/DESTRUCTIVE onay."""
    entry = _registry_entry(tool)
    if entry is None:
        return None
    level = int(entry.security)
    if level <= 1:
        return ALLOW, f"registry seviyesi {entry.security.name}: onaysız"
    return APPROVE, f"registry seviyesi {entry.security.name}: kullanıcı onayı"


@functools.lru_cache(maxsize=1)
def _model_live_names() -> frozenset[str]:
    """Modelin sesli araç yolundan (E1/E2) GERÇEKTEN erişebildiği adlar:
    TOOL_DECLARATIONS, _execute_tool dalları ve tools/ registry kayıtları.
    Kaynak dosyalar süreç başına bir kez okunur."""
    return frozenset(name for name, srcs in collect_reachable_tools().items()
                     if Source.MODEL_LIVE in srcs)


def _model_reachable(tool: str) -> bool:
    # Çalışma anında registry'ye eklenenler de modelin araç listesine girer.
    # discovered_* öneki tek başına yetmez (Adım 3.3): modelin uydurduğu bir
    # discovered_* adı fail-closed spec ile NEEDS_APPROVAL alıp onay kuyruğuna
    # giriyor ve bekleyen onayın önüne geçiyordu. Modelin gerçekten
    # erişebildiği discovered_* araçları (TOOL_DECLARATIONS) EXECUTE ile onay
    # ister; entegrasyonla eklenenler agent_loop yolunda fail-closed kalır.
    return tool in _model_live_names() or _registry_entry(tool) is not None


def authorize(tool: str, args: Mapping | None, source: Source) -> Decision:
    """Tek karar noktası. Kayıtsız araç DENY; okuma dışı varsayılan
    NEEDS_APPROVAL; modele giden hiçbir metinde onay kodu yoktur."""
    call = resolve(tool, args, source)
    if spec_for(tool) is None:
        return Decision(Verdict.DENY, call, "kayıtsız araç",
                        f"BLOCKED: '{tool}' kayıtlı bir araç değil; çağrılmadı.")
    if source is Source.MODEL_LIVE and not _model_reachable(tool):
        # Brain Team / agent_loop / CLI'nin iç araçları ve 'brain_team' sözde
        # aracı modelden çağrılamaz. DENY onay yuvasına dokunmaz: model
        # bekleyen bir onayı (ör. Brain adımı) kendi parmak iziyle ezemez.
        return Decision(Verdict.DENY, call, "modelin araç listesinde değil",
                        f"BLOCKED: '{tool}' bu yoldan çağrılabilen bir araç değil; çağrılmadı.")
    registry_policy = (_registry_policy(tool)
                       if source in (Source.MODEL_LIVE, Source.REACT) else None)
    if source is Source.MODEL_LIVE and tool in _LIVE_POLICY:
        policy_fn, reason = _LIVE_POLICY[tool]
        policy = policy_fn(call.params)
    elif registry_policy is not None:
        policy, reason = registry_policy
    else:
        policy = ALLOW if call.effect is Effect.READ else APPROVE
        reason = "okuma" if policy == ALLOW else "okuma dışı etki: onay gerekir"
    if policy == APPROVE:
        model_message, user_prompt = _approval_texts(call)
        return Decision(Verdict.NEEDS_APPROVAL, call, reason, model_message,
                        user_prompt=user_prompt, request_id=call.fingerprint)
    protocol = policy if policy in (TOOL_CODE, TERMINAL_CODE) else None
    return Decision(Verdict.ALLOW, call, reason, "", protocol=protocol)


# ── Yürütme: prepare / finish / execute ───────────────────────────────────

_CONFIRM_CODE_RE = re.compile(r"confirm_code\s*=\s*'?([0-9a-fA-F]+)'?")
_TERMINAL_CODE_RE = re.compile(r"^Onay kodu:\s*(\S+)\s*$", re.M)


def strip_terminal_code(text: str) -> tuple[str, str | None]:
    """terminal_tool önizlemesinden "Onay kodu: X" satırını (ve kodla tekrar
    çağır talimatını) çıkarır: (modele gidecek metin, kod)."""
    text = str(text or "")
    match = _TERMINAL_CODE_RE.search(text)
    if not (text.startswith("ONAY GEREKL") and match):
        return text, None
    lines = [ln for ln in text.splitlines()
             if not ln.startswith("Onay kodu:") and "confirm_code" not in ln]
    return "\n".join(lines), match.group(1)


def prepare(decision: Decision, grant: Grant | None) -> dict:
    """Araca gidecek parametreler. DENY ya da eşleşen Grant'ı olmayan
    NEEDS_APPROVAL için PermissionError - araç ÇAĞRILMAZ."""
    call = decision.call
    if decision.verdict is Verdict.DENY or call is None:
        raise PermissionError(decision.model_message or "Araç reddedildi.")
    if decision.verdict is Verdict.NEEDS_APPROVAL and (
            grant is None or grant.fingerprint != call.fingerprint):
        raise PermissionError(f"'{call.tool}' kullanıcı onayı olmadan çalıştırılamaz.")
    params = dict(call.params)
    if grant is not None and grant.code and grant.fingerprint == call.fingerprint:
        params["confirm_code"] = grant.code
    return params


def finish(decision: Decision, result: Any, store: ApprovalStore | None) -> Any:
    """Aracın önizleme çıktısındaki kodu saklar (store) ve modele KODSUZ bir
    önizleme + onay talimatı döner. Protokolü olmayan araçta sonuç aynen."""
    call = decision.call
    if decision.protocol == TERMINAL_CODE:
        text, code = strip_terminal_code(result)
        if code is None:
            return text
        if store is not None:
            store.remember_code(call, code)
        return (
            f"{text}\nKullanıcıya komutu TEK cümleyle anlat ve 'evet' ya da 'hayır' "
            "demesini iste. Kullanıcı bir sonraki mesajında onay verdikten SONRA "
            "terminal'i AYNI command ve cwd ile BİR KEZ tekrar çağır; onay gelmeden çağırma."
        )
    if decision.protocol == TOOL_CODE:
        text = str(result or "")
        head, sep, tail = text.partition("\n\n")
        match = _CONFIRM_CODE_RE.search(head)
        if not (head.startswith("ONAY GEREKL") and match):
            return result
        if store is not None:
            store.remember_code(call, match.group(1))
        preview = " ".join(
            sentence for sentence in re.split(r"(?<=\.)\s+", head)
            if "confirm_code" not in sentence and "kodu" not in sentence
        ).strip()
        instruction = (
            f"{preview} Kullanıcıya ne yapılacağını TEK cümleyle anlat ve 'evet' "
            "veya 'onaylıyorum' demesini iste. Kullanıcı bir sonraki mesajında "
            f"onay verdikten SONRA {call.tool}'ı AYNI parametrelerle BİR KEZ "
            "tekrar çağır; onay gelmeden çağırma."
        )
        return instruction + (sep + _CONFIRM_CODE_RE.sub("", tail) if tail else "")
    return result


# Etki -> denetim kaydindaki risk etiketi.
_EFFECT_RISK = {Effect.READ: "low", Effect.MUTATE: "medium", Effect.EXTERNAL: "high",
                Effect.EXECUTE: "high", Effect.SYSTEM: "high"}


def audit(decision: Decision, result: Any, *, executed: bool,
          grant: Grant | None = None) -> None:
    """Kapı kararını TEK denetim kaydına (JARVIS_HOME/memory/audit.log,
    core/audit_log.log_action biçimi) yazar: engellenen/onay bekleyen
    (executed=False) VE yürütülen (ALLOW ya da onaylı, executed=True) her
    çağrı. approved yalnızca gerçek kullanıcı onayı (Grant) ile yürütülen
    çağrıda True'dur. params maskelenir, parmak izi yalnızca hash."""
    from jarvis.core.audit_log import log_action
    call = decision.call
    # Onay bekleyen / engellenen çağrıda modele giden metin argümanları açık
    # yazar (ör. message_text); kayda yalnızca karar yazılır.
    text = str(result) if executed else f"{decision.verdict.value}: {decision.reason}"
    try:
        log_action(
            module="security_gate",
            action=call.tool if call else "?",
            detail=decision.reason,
            risk=_EFFECT_RISK.get(call.effect, "high") if call else "high",
            approval_required=decision.verdict is Verdict.NEEDS_APPROVAL,
            result=text[:200],
            source=call.source.value if call is not None and call.source else "?",
            verdict=decision.verdict.value,
            fingerprint=call.fingerprint if call else None,
            tool=call.tool if call else "?",
            params=dict(call.params) if call else {},
            approved=bool(executed and grant is not None),
            executed=bool(executed),
        )
    except Exception:
        pass   # denetim kaydı yazılamazsa araç akışı bozulmaz


def execute(decision: Decision, grant: Grant | None,
            runner: Callable[[dict], Any], store: ApprovalStore | None = None) -> Any:
    """Kararı uygular: yetki yoksa runner ÇAĞRILMAZ; aracın önizleme kodu
    modele gitmez."""
    params = prepare(decision, grant)
    return finish(decision, runner(params), store)
