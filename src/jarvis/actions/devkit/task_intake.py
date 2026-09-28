"""Görev kabulü: dev_agent planlamaya BAŞLAMADAN önce yapılan ucuz, modelsiz
kontroller.

NEDEN (2026-09-28, Windows canlı testi): kullanıcı "[Hedef URL] adresine git,
JavaScript çalıştıktan sonra sonsuz kaydırmayla gelen içeriği kazı" dedi.
  1. `[Hedef URL]` doldurulmamış bir şablondu; hiçbir katman fark etmedi ve
     uydurma bir adresle proje planlandı.
  2. Görev JavaScript/kaydırma istiyordu ama plan `requests` ile yapıldı;
     `requests` JS çalıştıramaz, yani program ne olursa olsun boş dönerdi.
Bu modül ikisini de deterministik olarak yakalar.
"""
from __future__ import annotations

import re

# Köşeli/açılı parantez içinde "doldurulacak yer" izlenimi veren kelimeler.
_PLACEHOLDER_WORDS = (
    "url", "adres", "link", "hedef", "site", "sayfa", "dosya", "file", "path", "yol",
    "klasör", "klasor", "folder", "isim", "name", "your", "buraya", "here",
    "email", "e-posta", "kullanıcı", "kullanici", "user", "token", "anahtar", "key",
)
# [Hedef URL], <URL>, {dosya_yolu} … — içinde { } , : olmayan kısa ifadeler.
# "[ {id, baslik} ]" gibi bir JSON şeması bu yüzden şablon sayılmaz.
_BRACKETED = re.compile(r"\[([^\[\]{}(),:\n]{1,40})\]|<([^<>{}(),:\n]{1,40})>|\{([^{}\[\](),:\n]{1,40})\}")
_ALLCAPS_PLACEHOLDER = re.compile(r"\b(?:YOUR|TARGET|HEDEF)_[A-Z_]{2,30}\b")

_URL = re.compile(
    r"(?:https?://|file://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|dev|app|co|tr|edu|gov|info|biz|me|ai)(?:\.[a-z]{2})?\b",
    re.IGNORECASE,
)
_URL_WORDS = re.compile(r"\b(url|adres(?:e|i|ine|inden|ten)?|link(?:e|i|ten)?|web ?sayfa\w*|site(?:ye|yi|sine|den)?)\b",
                        re.IGNORECASE)

_BROWSER_HINTS = re.compile(
    r"javascript|\bjs\b|infinite ?scroll|sonsuz kaydır|kaydır(?:ma|arak|ıldıkça|ildikce)|aşağı kaydır"
    r"|scroll|load more|daha fazla yükle|dinamik (?:sayfa|içerik|yüklen)|dynamic(?:ally)? (?:load|content|page)"
    r"|tarayıcı(?:yı|da)? (?:aç|otomasyon|kullan)|headless (?:browser|tarayıcı)|playwright|selenium"
    r"|butona? tıkla|click (?:the )?button|single[- ]page app|\bspa\b",
    re.IGNORECASE,
)
BROWSER_PACKAGES = ("playwright", "selenium", "pyppeteer")


def find_placeholders(text: str) -> list[str]:
    """Doldurulmamış şablon ifadelerini döndürür (ör. '[Hedef URL]')."""
    found: list[str] = []
    for m in _BRACKETED.finditer(text or ""):
        inner = next(g for g in m.groups() if g is not None).strip()
        low = inner.casefold()
        if inner and any(re.search(rf"(?<![a-zçğıöşü]){re.escape(w)}", low) for w in _PLACEHOLDER_WORDS):
            found.append(m.group(0))
    found.extend(_ALLCAPS_PLACEHOLDER.findall(text or ""))
    return list(dict.fromkeys(found))


def has_url(text: str) -> bool:
    return bool(_URL.search(text or ""))


def missing_inputs(description: str) -> list[str]:
    """Kullanıcıya SORULMASI gereken eksik bilgiler (boş liste = sorun yok)."""
    problems = [f"doldurulmamış şablon: {p}" for p in find_placeholders(description)]
    placeholder_url = any(re.search(r"url|adres|link|site|sayfa", p, re.IGNORECASE) for p in problems)
    if not placeholder_url and _URL_WORDS.search(description or "") and not has_url(description) \
            and needs_browser_or_web(description):
        problems.append("bir web adresinden söz ediliyor ama gerçek bir URL verilmemiş")
    return problems


def needs_browser(description: str) -> bool:
    """Görev JavaScript çalıştıran gerçek bir tarayıcı gerektiriyor mu?"""
    return bool(_BROWSER_HINTS.search(description or ""))


def needs_browser_or_web(description: str) -> bool:
    return needs_browser(description) or bool(
        re.search(r"kazı|scrap|crawl|web ?sayfa|siteden|sitesinden|indir(?:ip)? .*sayfa", description or "", re.IGNORECASE)
    )


def plan_uses_browser(plan: dict) -> bool:
    deps = " ".join(str(d) for d in plan.get("dependencies", []) or []).casefold()
    return any(pkg in deps for pkg in BROWSER_PACKAGES)


BROWSER_RULE = (
    "BROWSER REQUIRED: this task needs content that only appears after JavaScript runs "
    "(infinite scroll / 'load more' / dynamic page). Plain HTTP clients (requests, httpx, urllib, "
    "aiohttp) CANNOT execute JavaScript and will return an empty or partial page. You MUST use "
    "Playwright (sync API: from playwright.sync_api import sync_playwright), launch Chromium headless, "
    "wait for the content selector with the requested timeout, scroll / click 'load more' in a loop "
    "until enough items are loaded, and put \"playwright\" in \"dependencies\". "
    "Take the target URL as the first command-line argument (sys.argv[1])."
)
