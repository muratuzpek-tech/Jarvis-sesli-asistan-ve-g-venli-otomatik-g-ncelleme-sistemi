import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import re
import threading
import time
import sys
import struct
import traceback
from urllib.parse import unquote, urlparse
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
from jarvis.ui import JarvisUI
from jarvis.memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)

from jarvis.actions.file_processor import file_processor
from jarvis.actions.flight_finder import flight_finder
from jarvis.actions.open_app import open_app
from jarvis.actions.weather_report import weather_action
from jarvis.actions.send_message import send_message
from jarvis.actions.reminder import reminder
from jarvis.actions.computer_settings import computer_settings
from jarvis.actions.screen_processor import _capture_camera, _capture_screen
from jarvis.actions.youtube_video import youtube_video
from jarvis.actions.desktop import desktop_control
from jarvis.actions.browser_control import browser_control
from jarvis.actions.file_controller import file_controller
from jarvis.actions.code_helper import code_helper
from jarvis.actions.dev_agent import dev_agent
from jarvis.actions.web_search import web_search as web_search_action
from jarvis.actions.computer_control import computer_control
from jarvis.actions.game_updater import game_updater
from jarvis.actions.system_monitor import SystemMonitor, get_system_status
from jarvis.actions.proactive import ProactiveEngine
from jarvis.actions.automation import task_manager, pop_due_tasks
from jarvis.actions.pattern_tracker import log_tool_call, detect_patterns
from jarvis.actions.health_check import health_check
from jarvis.actions.agent_board import start_parallel_task, check_agent_board
from jarvis.actions.audio_devices import (
    device_identity as _audio_device_identity,
    device_name as _audio_device_name,
    get_candidates as _audio_candidates,
    list_devices as _list_audio_devices,
    resolve_device_index as _resolve_audio_device,
)
from jarvis.actions.resilience import CircuitBreaker
from jarvis.actions.self_improve import self_improve
from jarvis.actions.agent_loop import agent_loop_tool, start_background_loop as start_agent_loop
from jarvis.actions.conversation_log import log_turn, recall_conversation
from jarvis.actions.github_arama import github_search
from jarvis.actions.discovered_topydo import run as discovered_topydo_run
from jarvis.actions.discovered_jc import run as discovered_jc_run
from jarvis.actions.intent_router import match_system_read, match_file_analysis, match_file_modification
from jarvis.core.secure_config import api_keys_path
from jarvis.paths import asset


# --- GitHub-uzerinden-kendini-gelistir icin DETERMINISTIK kisa devre ------
# Canli modelin (Gemini Live) arac secimi, "GitHub'da kendine yeni bir
# yetenek bul" turu istekleri guvenilir sekilde agent_loop'a yonlendiremedi -
# canli testte defalarca web_search'e kaydigi gozlemlendi (tool description
# metinlerini guclendirmek yeterli olmadi). Bu yuzden bu ozel durum icin
# modelin kararina GUVENMIYORUZ: metinde hem "github" hem de kendine/kendi
# gelisimi icin gibi bir referans varsa, dogrudan (modelsiz) agent_loop'a
# yonlendiriyoruz. Sadece "github'da X ara" gibi normal, kendine-referans
# ICERMEYEN aramalar bundan ETKILENMEZ - onlar normal sekilde modele gidip
# github_arama'ya yonlenmeye devam eder.
_GITHUB_SELF_IMPROVE_HINTS = (
    "kendine", "kendin için", "kendi kendine", "kendi gelişim", "kendini geliştir",
    "kendisi için", "kendi geliş", "senin gelişimin", "senin gelişim",
)


def _is_github_self_improve_request(text: str) -> bool:
    t = (text or "").lower()
    if "github" not in t:
        return False
    return any(hint in t for hint in _GITHUB_SELF_IMPROVE_HINTS)


def _is_generic_task_request(text: str) -> bool:
    t = (text or "").casefold().strip()
    return any(e in t for e in ["görev kuyruğuna ekle", "görev listeme ekle", "arka planda çalıştır"])



def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = api_keys_path()
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 2048 # AirPods Pro Koruma Ayari


def _pcm_rms_level(data: bytes, max_expected: float = 9000.0) -> float:
    """Normalize signed-int16 mono PCM for the Voice Assistant spectrum HUD."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
        return 0.0
    count = len(data) // 2
    try:
        samples = struct.unpack(f"<{count}h", data[:count * 2])
    except struct.error:
        return 0.0
    mean_square = sum(sample * sample for sample in samples) / count
    return max(0.0, min(1.0, (mean_square ** 0.5) / max_expected))

# Art arda basarisiz mikrofon/hoparlor denemelerinde her reconnect'te tum
# cihaz listesini bastan taramak yerine kisa bir sure hizlica pes eder
# (resilience.py'deki devre kesici deseniyle tutarli).
_mic_breaker      = CircuitBreaker(name="mikrofon", failure_threshold=3, cooldown_seconds=30.0)
_speaker_breaker  = CircuitBreaker(name="hoparlör", failure_threshold=3, cooldown_seconds=30.0)

def _get_api_key() -> str:
    from jarvis.core.secure_config import get_gemini_api_key
    return get_gemini_api_key()


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)
_NON_LATIN_RE = re.compile(
    r"[\u0370-\u052f\u0590-\u08ff\u0900-\u0dff\u1100-\u11ff\u2e80-\u9fff]"
)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"\s*(?:<\s*noise\s*>|\[\s*noise\s*\])\s*", " ", text, flags=re.IGNORECASE)
    # The workstation is configured for Turkish. Live ASR can occasionally
    # hallucinate a foreign-script fragment from ambient audio; discard that
    # fragment before it reaches routing or the model response.
    text = _NON_LATIN_RE.sub(" ", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _dedupe_response(text: str) -> str:
    """Collapse a complete response accidentally emitted twice by Live."""
    text = re.sub(r"\s+", " ", text or "").strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) >= 2 and len(sentences) % 2 == 0:
        midpoint = len(sentences) // 2
        if sentences[:midpoint] == sentences[midpoint:]:
            return " ".join(sentences[:midpoint]).strip()
    if len(text) >= 20 and len(text) % 2 == 0:
        half = len(text) // 2
        if text[:half].strip(" .!?\n") == text[half:].strip(" .!?\n"):
            return text[:half].strip()
    return text


def _response_audio_data(response) -> bytes | None:
    """Extract inline data without triggering google-genai's warning property."""
    server_content = (
        response.get("server_content")
        if isinstance(response, dict)
        else getattr(response, "server_content", None)
    )
    model_turn = (
        server_content.get("model_turn")
        if isinstance(server_content, dict)
        else getattr(server_content, "model_turn", None)
    )
    parts = (
        model_turn.get("parts")
        if isinstance(model_turn, dict)
        else getattr(model_turn, "parts", None)
    ) or []
    data_parts: list[bytes] = []
    for part in parts:
        inline_data = (
            part.get("inline_data")
            if isinstance(part, dict)
            else getattr(part, "inline_data", None)
        )
        data = (
            inline_data.get("data")
            if isinstance(inline_data, dict)
            else getattr(inline_data, "data", None)
        )
        if isinstance(data, bytes):
            data_parts.append(data)
    return b"".join(data_parts) or None


def _close_audio_stream(stream) -> None:
    """Best-effort stop/close used after task cancellation or open failure."""
    if stream is None:
        return
    for method_name in ("stop", "close"):
        method = getattr(stream, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass


def _is_auth_error(error_text: str) -> bool:
    text = (error_text or "").casefold()
    return any(marker in text for marker in (
        "api key not valid", "api_key_invalid", "invalid api key",
        "unauthenticated", "permission_denied", "permission denied",
        "authentication", "401", "403", "1007",
    ))

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items). "
            "Do NOT use this for searching GitHub repositories specifically (use github_arama "
            "instead), and do NOT use this if the user is asking Jarvis to find/search for a "
            "new capability or tool FOR ITSELF, e.g. 'kendine github'da bir şey bul', "
            "'kendini geliştirmek için github'ı tara' — that always goes through agent_loop "
            "(action=add), never web_search."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic"},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "system_scan_and_repair",
        "description": (
            "Scans every project Python module by actually importing it in an isolated "
            "subprocess to catch real runtime and missing-dependency errors, checks every "
            "pyproject.toml-declared dependency's installed version for compatibility, and "
            "automatically installs or upgrades anything missing or incompatible via pip "
            "without asking for confirmation. Use when the user asks to check if the system "
            "or project is healthy, scan for missing dependencies, or self-repair."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": (
                    "volume_up | volume_down | mute | unmute | volume_set (use with value=0-100) | "
                    "brightness_up | brightness_down | sleep_display | pause_video | close_app | "
                    "close_window | fullscreen | minimize | maximize | snap_left | snap_right | "
                    "switch_window | show_desktop | task_manager | focus_search | refresh_page | "
                    "close_tab | new_tab | next_tab | prev_tab | go_back | go_forward | zoom_in | "
                    "zoom_out | zoom_reset | find_on_page | scroll_up | scroll_down | scroll_top | "
                    "scroll_bottom | page_up | page_down | copy | paste | cut | undo | redo | "
                    "select_all | save | enter | escape | screenshot | lock_screen | open_settings | "
                    "file_explorer | open_run | dark_mode | toggle_wifi | restart | shutdown | "
                    "type_text (use with value=text) | press_key (use with value=key name) | "
                    "reload_n (use with value=integer). For volume, ALWAYS use volume_set with an "
                    "explicit 0-100 value when the user gives a percentage/level; only use "
                    "volume_up/volume_down for relative 'louder/quieter' requests."
                )},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level 0-100 for volume_set, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, delete_all_files, move, copy, rename, read, write, find, disk usage. delete_all_files is preview-only first and requires explicit user confirmation with confirm_code; it moves direct files to Trash, never the folder or subfolders. IMPORTANT for 'move': the first call (no confirm_code) never moves anything, it only returns a preview and a confirm_code. You MUST relay the exact source and destination to the user and wait for their explicit confirmation in their next message before calling 'move' again with that confirm_code. Never chain both calls in the same turn without a real user confirmation in between.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":       {"type": "STRING", "description": "list | create_file | create_folder | delete | delete_all_files | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info | extract"},
                "path":         {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination":  {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":     {"type": "STRING", "description": "New name for rename"},
                "content":      {"type": "STRING", "description": "Content for create_file/write"},
                "name":         {"type": "STRING", "description": "File name to search for"},
                "extension":    {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":        {"type": "INTEGER", "description": "Number of results for largest"},
                "confirm_code": {"type": "STRING", "description": "Only for action=move or action=delete_all_files: the code returned by a PRIOR unconfirmed call, after the user has explicitly confirmed. Leave empty on the first attempt."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "task_manager",
        "description": "Schedules a Jarvis command to run automatically at a future date/time (add), lists scheduled tasks (list), or cancels one (remove). Scheduled commands are actually re-sent to you (Jarvis) when their time comes, exactly as if the user had said them live — plan and execute them for real at that moment, including calling any other tools needed.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":           {"type": "STRING", "description": "add | list | remove"},
                "minutes_from_now": {"type": "INTEGER", "description": "For action=add, PREFERRED for relative delays (e.g. user says 'in 2 minutes', 'in an hour'): number of minutes from now. The exact target time is computed reliably in code, not by you — never compute or guess the clock time yourself for relative requests."},
                "run_at":           {"type": "STRING", "description": "For action=add, ONLY use this for an explicit absolute date/time the user gave (e.g. 'Dec 31 at 18:00'): 'YYYY-MM-DD HH:MM' format. Do not use this for relative delays like 'in N minutes' — use minutes_from_now instead."},
                "command":          {"type": "STRING", "description": "For action=add: the natural-language command to run at that time, e.g. 'masaüstünü organize et'."},
                "task_id":          {"type": "STRING", "description": "For action=remove: the task id to cancel."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "health_check",
        "description": "Runs a full self-diagnostic: checks Ollama connection, microphone signal, memory file, Python file syntax, and internet connectivity. Use this whenever the user asks you to check yourself, run a health check, or says something isn't working and you need to diagnose why.",
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "start_parallel_task",
        "description": "Starts a dev_agent coding task in the BACKGROUND and returns immediately without waiting — lets the user keep talking to you while the project is being built. Use this when the user wants to run multiple coding tasks at once, or explicitly asks for something to run 'in the background' / 'paralel' / 'arka planda'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What to build, same as for dev_agent."},
                "language":     {"type": "STRING", "description": "Programming language, default python."},
                "project_name": {"type": "STRING", "description": "Optional project folder name."},
            },
            "required": ["description"]
        }
    },
    {
        "name": "check_agent_board",
        "description": "Shows the status of all background tasks started with start_parallel_task — which are pending, running, completed, or failed. Use when the user asks to check on background tasks, or see the 'board'/'pano'.",
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors. The first call (no confirm_code) never installs or runs anything, it only returns a preview and a confirm_code. You MUST relay what will be built to the user and wait for their explicit confirmation in their next message before calling 'dev_agent' again with that confirm_code. Never chain both calls in the same turn without a real user confirmation in between.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
                "confirm_code": {"type": "STRING", "description": "The code returned by a PRIOR unconfirmed call, after the user has explicitly confirmed. Leave empty on the first attempt."},
            },
            "required": ["description"]
        }
    },
    {
        "name": "self_improve",
        "description": (
            "Autonomously improves ONE of Jarvis's OWN code files (never an external/downloaded "
            "file): takes a full project backup first, asks the model for an improved version of "
            "the file, then VERIFIES the result (syntax check + real import in a separate process) "
            "before keeping it. If verification fails it retries up to 3 times, then restores the "
            "original file automatically. Use this when the user asks Jarvis to improve/refactor "
            "its own code, fix itself, or 'kendini geliştir'. If no file is named, picks the "
            "actions/ file that hasn't been reviewed the longest. The first call (no confirm_code) "
            "never touches the file, it only returns a preview and a confirm_code. You MUST relay "
            "which file and goal to the user and wait for their explicit confirmation in their "
            "next message before calling 'self_improve' again with that confirm_code. Never chain "
            "both calls in the same turn without a real user confirmation in between."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path":    {"type": "STRING", "description": "Optional path (relative to the project root) of the file to improve, e.g. 'actions/weather_report.py'. If omitted, Jarvis picks one itself."},
                "goal":         {"type": "STRING", "description": "Optional description of what to improve (e.g. 'add better error handling'). Defaults to a general code-quality pass."},
                "confirm_code": {"type": "STRING", "description": "The code returned by a PRIOR unconfirmed call, after the user has explicitly confirmed. Leave empty on the first attempt."},
            },
            "required": []
        }
    },
    {
        "name": "agent_loop",
        "description": (
            "Manages Jarvis's background autonomous task list — a loop that runs on its own "
            "every ~60 seconds, working one step at a time toward goals the user adds, using "
            "only Jarvis's existing safe tools (never arbitrary code/commands). Use action=add "
            "when the user asks Jarvis to handle/work on something in the background over time "
            "(e.g. 'bunu arka planda halllet', 'görev listeme ekle'). ALSO use action=add "
            "(NEVER web_search or github_arama directly) whenever the user asks Jarvis to "
            "search GitHub on its OWN BEHALF for a new capability/tool for ITSELF, e.g. "
            "'kendine github'da bir araç bul', 'kendini geliştirmek için github'ı tara', "
            "'github'dan kendine yeni bir şey bul, bulursan bana sor' — put the search topic "
            "into the goal text (e.g. goal='GitHub'da basit bir todo list aracı bul ve "
            "entegre etmeyi öner'). The background loop will repeatedly search/analyze "
            "candidates on its own and will ALWAYS ask for approval before integrating "
            "anything — it never does so silently. Use action=list when the "
            "user asks what background tasks are pending or what's awaiting approval. "
            "CRITICAL: if a task is 'awaiting_approval' (a destructive step like deleting a file, "
            "shutting down, or sending a message is waiting for permission) and the user says "
            "something like 'onaylıyorum'/'evet yap'/'approve it' in that context, call this with "
            "action=approve and that task's id. If they say 'iptal et'/'hayır'/'deny', use "
            "action=deny with that task's id."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "add | list | approve | deny"},
                "goal":    {"type": "STRING", "description": "The goal to work toward in the background (action=add only)."},
                "task_id": {"type": "STRING", "description": "The short task id to approve/deny (action=approve or action=deny only)."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "recall_conversation",
        "description": (
            "Recalls and summarizes past conversation with the user from a real, persisted "
            "conversation log — use this whenever the user asks what was discussed before, "
            "e.g. 'dün ne konuştuk', 'geçen hafta ne demiştim', 'bunu daha önce konuşmuş "
            "muyduk', 'what did we talk about yesterday', OR a recent/same-session reference "
            "like 'demin ne dedin', '10 dakika önce ne konuşmuştuk', 'az önce söyledin ama' — "
            "for those, use the 'minutes' parameter instead of 'period'. Never say you don't "
            "have access to past conversations without calling this tool first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "minutes": {"type": "INTEGER", "description": "Use for recent/same-session references ('demin', 'az önce', 'X dakika önce', 'biraz önce'): how many minutes back to search, e.g. 10. Takes priority over period/date when set."},
                "period": {"type": "STRING", "description": "bugun | dun | bu_hafta | gecen_hafta — use this for day-level relative time references."},
                "date":   {"type": "STRING", "description": "Optional explicit date in YYYY-MM-DD format instead of period."},
                "topic":  {"type": "STRING", "description": "Optional — focus the summary on a specific topic if the user mentioned one."},
            },
            "required": []
        }
    },
    {
        "name": "github_arama",
        "description": (
            "Searches GitHub's real, public repository search API (read-only — never clones, "
            "downloads, or installs anything) and reports results back to the user directly. "
            "Use this ONLY for a one-off lookup the user wants to see the results of themselves, "
            "e.g. 'github'da X ara', 'github'da bununla ilgili bir proje var mı bak', "
            "'search github for X'. Do NOT use this if the user is asking Jarvis to find a new "
            "capability/tool for ITSELF (for self-improvement) — that goes through agent_loop "
            "(action=add) instead, never this tool directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":       {"type": "STRING", "description": "What to search for on GitHub."},
                "min_stars":   {"type": "INTEGER", "description": "Optional minimum star count filter."},
                "max_results": {"type": "INTEGER", "description": "Optional max number of results (default a small handful)."},
            },
            "required": ["query"]
        }
    },
    {
        "name": "discovered_topydo",
        "description": (
            "Simple to-do / task list tool (todo.txt format) — use whenever the user wants to "
            "add, list, complete, delete, prioritize, or clear items on a personal to-do list, "
            "e.g. 'yapılacaklar listeme ekle', 'listeme süt al yaz', 'görevlerimi göster', "
            "'şu görevi tamamladım', 'listemi temizle', 'to-do list', 'add a task'. This is a "
            "separate, lightweight list from agent_loop's background task queue — use this one "
            "for the user's own personal to-do items, not for background automation steps."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "add|list|done|delete|prioritize|clear (accepts Turkish synonyms like ekle/listele/bitir/sil/oncelik/temizle too)."},
                "task":     {"type": "STRING", "description": "The task text (for action=add)."},
                "priority": {"type": "STRING", "description": "Optional priority letter A-Z (for action=add or action=prioritize)."},
                "task_id":  {"type": "INTEGER", "description": "The item's list number, as shown by action=list (for action=done/delete/prioritize)."},
                "filter":   {"type": "STRING", "description": "Optional search/filter text (for action=list)."},
                "all":      {"type": "BOOLEAN", "description": "If true with action=list, also show already-completed items."},
            },
            "required": ["action"]
        }
    },
    {
        "name": "discovered_jc",
        "description": (
            "Converts the plain-text output of a standard command-line tool (e.g. 'ls', 'ps', "
            "'df', 'ifconfig', 'netstat') into structured JSON, using the open-source 'jc' "
            "parser library. Use this ONLY if the user explicitly gives you raw command output "
            "and asks you to parse/structure it into JSON — this is a low-level utility, not a "
            "general system-info tool (use system_status or windows_system for that instead)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "The name of the command whose output is being parsed, e.g. 'ls', 'ps', 'df' (jc parser name, hyphens allowed)."},
                "data":    {"type": "STRING", "description": "The raw text output of that command to parse."},
            },
            "required": ["command", "data"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

# --- Plugin system ---


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _log_send_result(self, fut, label: str) -> None:
        # TEŞHİS AMAÇLI GÖZLEMLENEBİLİRLİK (2026-09-16) — mevcut davranışı
        # DEĞİŞTİRMEZ, sadece send_client_content() coroutine'inin gerçekten
        # başarılı mı yoksa sessizce mi hata verdiğini görünür kılar.
        try:
            exc = fut.exception()
        except Exception as e:
            exc = e
        if exc:
            msg = f"ERR: [{label}] send_client_content başarısız: {exc}"
            print(f"[JARVIS] ❌ {msg}")
            try:
                self.ui.write_log(msg)
            except Exception:
                pass
        else:
            ok_msg = f"SYS: [{label}] send_client_content tamamlandı (hatasız)."
            print(f"[JARVIS] ✅ {ok_msg}")
            try:
                self.ui.write_log(ok_msg)
            except Exception:
                pass

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        try:
            log_turn("user", text)
        except Exception as e:
            print(f"[JARVIS] ⚠️ conversation_log (text command): {e}")

        # BRAIN_TEAM_HEALTH: "AI takımının sağlık durumu" gibi açık sağlık
        # isteklerini genel Gemini sohbetine bırakma.  Bu, daha önce komutun
        # "bu yeteneğim yok" cevabına düşmesine neden oluyordu; Brain Team'in
        # gerçek heartbeat endpoint'ine deterministik olarak yönlendir.
        normalized = text.casefold()
        # Copied Explorer paths are local file requests, not Brain Team goals.
        # Handle file:///C:/... before generic task routing so Gemini 503/429
        # cannot block a deterministic local inspection.
        local_file = None
        if "file://" in normalized:
            start = normalized.find("file://")
            raw_uri = text[start:].split()[0].strip("\"'`.,")
            parsed = urlparse(raw_uri)
            local_file = unquote(parsed.path or "")
            if parsed.netloc and not local_file.startswith("/"):
                local_file = f"//{parsed.netloc}/{local_file}"
            if len(local_file) >= 3 and local_file.startswith("/") and local_file[2] == ":":
                local_file = local_file[1:]
        elif re.search(r"(?:[A-Za-z]:[\\/]|/home/|/Users/).+\.(?:zip|txt|pdf|docx?|xlsx?|py|json)$", text, re.I):
            local_file = text.strip().strip("\"'`.,")
        if local_file:
            try:
                from jarvis.actions.file_controller import get_file_info
                info = get_file_info(local_file)
                self.ui.write_log(f"[LOCAL_FILE_INFO] {info}")
                self.speak(f"[LOCAL_FILE_INFO_SONUC] Yerel dosya bilgisi: {info}. Dosyayı değiştirmedim.")
            except Exception as e:
                self.ui.write_log(f"[LOCAL_FILE_INFO_HATA] {e}")
                self.speak(f"Yerel dosya bilgisi alınamadı: {e}")
            return
        health_terms = (
            "ai takım", "beyin takımı", "brain team", "ajan takım",
            "agent takım", "ajanların sağlık", "ajanlarin saglik",
        )
        team_terms = ("ai takım", "beyin takımı", "brain team", "ajan takım")
        task_terms = ("görev", "gorev", "araştır", "arastır", "araştir", "özet", "ozet", "incele", "analiz", "denetim")
        explicit_task_request = (
            any(term in normalized for term in team_terms)
            and any(term in normalized for term in task_terms)
            and any(term in normalized for term in ("ver", "başlat", "baslat", "gönder", "gonder", "hazırla", "hazirla"))
        )
        status_terms = ("görev durumu", "gorev durumu", "çalışma ne durumda", "calisma ne durumda", "neden beklemede")
        explicit_status_request = any(term in normalized for term in status_terms)
        health_intent = (
            any(term in normalized for term in health_terms)
            and any(term in normalized for term in ("sağlık", "saglik", "health", "durumu", "durumunu", "heartbeat"))
            and not explicit_task_request
            and not explicit_status_request
        )
        if health_intent:
            try:
                from jarvis.core.brain_orchestrator import brain_team_tool
                health_result = brain_team_tool({"action": "health"}, player=self)
                self.ui.write_log(f"[BRAIN_TEAM_HEALTH] {health_result}")
                self.speak(
                    f"[BRAIN_TEAM_HEALTH_SONUC] Gerçek AI Beyin Takımı heartbeat sonucu: "
                    f"{health_result}. Her beyni ve durumunu kullanıcıya kısa ve doğal "
                    f"Türkçe ile özetle; ham teknik JSON okuma."
                )
            except Exception as e:
                error = f"AI Beyin Takımı sağlık kontrolü çalıştırılamadı: {e}"
                self.ui.write_log(f"[BRAIN_TEAM_HEALTH_HATA] {error}")
                self.speak(f"[BRAIN_TEAM_HEALTH_HATA] {error}")
            return

        # BRAIN_TEAM_START: Kullanıcı açıkça "AI takımına ... görevi ver"
        # dediğinde isteği genel Gemini araç seçimine bırakma.  Aksi halde
        # model bunu Agent Loop/task_manager'a yönlendirebiliyor; görev 60sn
        # kuyruğunda beklerken gerçek Brain Team hiç başlamıyordu.  Araştırma,
        # analiz ve özet gibi salt-okunur hedefleri doğrudan Brain Team'in
        # planner -> research -> security -> auditor zincirine gönder.
        if (
            any(term in normalized for term in team_terms)
            and any(term in normalized for term in task_terms)
            and not explicit_status_request
        ):
            try:
                from jarvis.core.brain_orchestrator import brain_team_tool
                goal = text.strip()
                result = brain_team_tool({"action": "start", "goal": goal}, player=self)
                self.ui.write_log(f"[BRAIN_TEAM_START] {result}")
                self.speak(
                    f"[BRAIN_TEAM_START_SONUC] Gerçek AI Beyin Takımı görevi başlatıldı: "
                    f"{result}. Kullanıcıya görevin arka planda çalıştığını ve sonucu "
                    f"tamamlanınca bildireceğimi kısa Türkçe ile söyle; sonuç uydurma."
                )
            except Exception as e:
                error = f"AI Beyin Takımı görevi başlatılamadı: {e}"
                self.ui.write_log(f"[BRAIN_TEAM_START_HATA] {error}")
                self.speak(f"[BRAIN_TEAM_START_HATA] {error}")
            return

        # Görev durumunu soran açık ifadeler de genel system_status'a değil,
        # Brain Team'in kalıcı görev kuyruğuna yönlendirilsin.
        if any(term in normalized for term in status_terms):
            try:
                from jarvis.core.brain_orchestrator import brain_team_tool
                result = brain_team_tool({"action": "status"}, player=self)
                self.ui.write_log(f"[BRAIN_TEAM_STATUS] {result}")
                self.speak(f"[BRAIN_TEAM_STATUS_SONUC] Gerçek görev kuyruğu sonucu: {result}. Bunu doğal Türkçe ile özetle.")
            except Exception as e:
                error = f"AI Beyin Takımı görev durumu alınamadı: {e}"
                self.ui.write_log(f"[BRAIN_TEAM_STATUS_HATA] {error}")
                self.speak(f"[BRAIN_TEAM_STATUS_HATA] {error}")
            return

        # TASK_ADD must precede deterministic system-read shortcuts. Otherwise
        # a sentence such as "system statusu kontrol et ve sonucu görev
        # kaydına yaz" is consumed as a direct read and the model may write a
        # file instead of creating the requested background task.
        if _is_generic_task_request(text) and not explicit_task_request:
            try:
                result = agent_loop_tool(parameters={"action": "add", "goal": text})
            except Exception as e:
                result = f"Görev eklenirken hata oluştu: {e}"
            self.ui.write_log(f"[AGENT_LOOP_EKLENDI] {result}")
            try:
                log_turn("jarvis", result)
            except Exception as e:
                print(f"[JARVIS] ⚠️ conversation_log (generic task priority): {e}")
            self.speak(
                f"[AGENT_LOOP_EKLENDI] Gerçek görev kuyruğu sonucu: {result}. "
                "Görevin arka planda işleneceğini kısa ve doğal Türkçe ile bildir; "
                "henüz sonuç uydurma ve dosyaya doğrudan yazma."
            )
            return

        # SYSTEM_READ -> windows_system -> process_list (Aşama 2 / Adım 1):
        # basit, deterministik bir eşleşme varsa Gemini'nin kendi araç
        # seçimine (system_status vb.) HİÇ bırakmadan doğrudan mevcut
        # tools_kopru.ALLOWED_TOOLS["windows_system"] üzerinden çalıştır.
        # Salt-okunur, parametresiz, tek komut (process_list) - dosya
        # değiştirme/işlem sonlandırma/admin yok. Brain Team/Agent Loop
        # zincirine hiç girmez.
        matched_command = match_system_read(text)
        if matched_command:
            try:
                from jarvis.actions.tools_kopru import ALLOWED_TOOLS
                raw_result = ALLOWED_TOOLS["windows_system"]({"command_name": matched_command})
                error_text = None
            except Exception as e:
                raw_result = None
                error_text = f"{e}"
            if error_text:
                summary_for_log = f"[SYSTEM_READ] {matched_command} çalıştırılırken hata: {error_text}"
            else:
                summary_for_log = f"[SYSTEM_READ] {matched_command} çalıştırıldı: {raw_result}"
            self.ui.write_log(summary_for_log)
            try:
                log_turn("jarvis", summary_for_log)
            except Exception as e:
                print(f"[JARVIS] ⚠️ conversation_log (system_read short-circuit): {e}")
            if error_text:
                prompt = (
                    f"[SYSTEM_READ_HATA] Kullanıcı çalışan işlemleri (process_list) sordu, "
                    f"ancak doğrudan çalıştırma hata verdi: \"{error_text}\". Bunu kullanıcıya "
                    f"açıkça, sessiz kalmadan bildir - teknik ayrıntıyı kısaca özetle."
                )
            else:
                prompt = (
                    f"[SYSTEM_READ_SONUC] Kullanıcı çalışan işlemleri sordu. Bu istek, Gemini'nin "
                    f"ayrı bir araç seçmesine gerek kalmadan doğrudan windows_system -> "
                    f"process_list ile ÇALIŞTIRILDI. Gerçek JSON sonucu: {raw_result}. Bunu "
                    f"kullanıcıya doğal, kısa bir dille özetle - varsa en çok CPU/RAM kullanan "
                    f"birkaç süreci öne çıkar, ham JSON'u okuma. Sonuç ok:false ise hatayı açıkça "
                    f"söyle, sessiz kalma."
                )
            self.speak(prompt)
            return

        # FILE_ANALYSIS -> brain_team -> coder_ai -> operation=analyze
        # (Aşama 2 / Adım 2): dosya yolu + analiz/inceleme/okuma/açıklama
        # niyeti AÇIKÇA algılanıyorsa, Gemini'nin code_helper / self_improve /
        # brain_team arasında serbestçe seçim yapmasına HİÇ bırakmadan,
        # DOĞRUDAN ve DETERMİNİSTİK olarak coder_ai'ye operation="analyze"
        # (dry_run - dosya ASLA değiştirilmez) ile bir görev oluştur. Planlama
        # LLM'ine (planner_ai) HİÇ gidilmiyor - "operation" burada, planner'ın
        # olası "modify" varsayılanına düşme riski OLMADAN, deterministik
        # olarak sabitleniyor. Mevcut görev/adım motoru (task_manager,
        # _tick/_execute_step/_finish_step, Auditor, Security, _notify_result)
        # HİÇ DEĞİŞTİRİLMEDEN aynen kullanılıyor.
        # FILE_MODIFICATION -> brain_team -> executor_ai -> file_controller
        # Acik dosya olusturma/yazma komutlarini Gemini'nin arac secimine
        # birakmadan deterministik olarak mevcut Brain Team gorev motoruna ver.
        file_mod = match_file_modification(text)
        if file_mod:
            try:
                from jarvis.core.brain_orchestrator import get_orchestrator

                orch = get_orchestrator()
                orch._last_player = self

                action = file_mod.get("action")
                path = file_mod.get("path", ".")
                name = file_mod.get("name", "")
                content = file_mod.get("content", "")

                description = text

                task = orch.tasks.create(
                    name=f"[FILE_MODIFICATION] {text}"[:100],
                    agent="executor_ai",
                    priority="medium",
                    payload={
                        "goal": text,
                        "plan": [{
                            "order": 1,
                            "description": description,
                            "agent": "executor_ai",
                            "operation": "execute",
                            "priority": "medium",
                            "file_path": "",
                        }],
                        "step_index": 0,
                        "history": [],
                        "audit_retries": 0,
                        "file_modification": {
                            "action": action,
                            "path": path,
                            "name": name,
                            "content": content,
                        },
                    },
                )

                # _execute_step() legacy metin sezgisinden de bagimsiz olarak
                # planner adiminin tasimadigi explicit parametreleri task goal
                # bilgisinden yararlanarak korur.
                summary_for_log = (
                    f"[FILE_MODIFICATION] '{name}' icin {action} gorevi olusturuldu "
                    f"(id={task['id']})."
                )
                error_text = None
            except Exception as e:
                summary_for_log = None
                error_text = f"{e}"

            self.ui.write_log(
                summary_for_log
                if not error_text
                else f"[FILE_MODIFICATION] Gorev olusturulamadi: {error_text}"
            )
            try:
                log_turn(
                    "jarvis",
                    summary_for_log or f"[FILE_MODIFICATION] hata: {error_text}",
                )
            except Exception as e:
                print(f"[JARVIS] conversation_log (file_modification): {e}")

            if error_text:
                prompt = (
                    f"[FILE_MODIFICATION_HATA] Kullanici istegi gorev olarak "
                    f"olusturulamadi: {error_text}. Bunu kullaniciya kisa ve "
                    f"acik bicimde bildir."
                )
            else:
                prompt = (
                    f"[FILE_MODIFICATION_BASLATILDI] Kullanici istegi mevcut "
                    f"AI Beyin Takimi'na gonderildi (dosya: {name}, islem: {action}). "
                    f"Gorev arka planda gerceklestirilecek; henuz sonucu uydurma, "
                    f"sadece gorevin baslatildigini bildir."
                )

            self.speak(prompt)
            return

        resolved_file_path = match_file_analysis(text, Path(__file__).resolve().parent)
        if resolved_file_path:
            try:
                from jarvis.core.brain_orchestrator import get_orchestrator
                orch = get_orchestrator()
                orch._last_player = self
                task = orch.tasks.create(
                    name=f"[FILE_ANALYSIS] {text}"[:100],
                    agent="coder_ai",
                    priority="low",
                    payload={
                        "goal": text,
                        "plan": [{
                            "order": 1,
                            "description": text,
                            "agent": "coder_ai",
                            "operation": "analyze",
                            "priority": "low",
                            "file_path": resolved_file_path,
                        }],
                        "step_index": 0,
                        "history": [],
                        "audit_retries": 0,
                    },
                )
                summary_for_log = (
                    f"[FILE_ANALYSIS] '{resolved_file_path}' için analiz görevi oluşturuldu "
                    f"(id={task['id']}) -> brain_team -> coder_ai -> operation=analyze."
                )
                error_text = None
            except Exception as e:
                summary_for_log = None
                error_text = f"{e}"
            self.ui.write_log(summary_for_log if not error_text else f"[FILE_ANALYSIS] Görev oluşturulamadı: {error_text}")
            try:
                log_turn("jarvis", summary_for_log or f"[FILE_ANALYSIS] hata: {error_text}")
            except Exception as e:
                print(f"[JARVIS] ⚠️ conversation_log (file_analysis short-circuit): {e}")
            if error_text:
                prompt = (
                    f"[FILE_ANALYSIS_HATA] Kullanıcı '{resolved_file_path}' dosyasının analiz "
                    f"edilmesini istedi, ancak görev oluşturulurken hata oluştu: \"{error_text}\". "
                    f"Bunu kullanıcıya açıkça, sessiz kalmadan bildir."
                )
            else:
                prompt = (
                    f"[FILE_ANALYSIS_BASLATILDI] Kullanıcının '{resolved_file_path}' dosyasını "
                    f"analiz etme isteği AI Beyin Takımı'na (coder_ai, SADECE OKUMA/analiz - dosya "
                    f"asla değiştirilmeyecek) gönderildi. Kullanıcıya kısaca, doğal bir dille, "
                    f"analizin arka planda yapılacağını ve sonucun birazdan bildirileceğini söyle - "
                    f"şimdi analiz sonucunu UYDURMA, henüz gelmedi."
                )
            self.speak(prompt)
            return

        # GENEL TASK_ADD: açıkça görev ekleme istendiğinde Gemini'nin serbest
        # araç seçimine bırakmadan gerçek Agent Loop kaydını hemen oluştur.
        if _is_generic_task_request(text) and not explicit_task_request:
            try:
                result = agent_loop_tool(parameters={"action": "add", "goal": text})
            except Exception as e:
                result = f"Görev eklenirken hata oluştu: {e}"
            self.ui.write_log(f"[AGENT_LOOP_EKLENDI] {result}")
            try:
                log_turn("jarvis", result)
            except Exception as e:
                print(f"[JARVIS] ⚠️ conversation_log (generic task): {e}")
            self.speak(
                f"[AGENT_LOOP_EKLENDI] Gerçek görev kuyruğu sonucu: {result}. "
                "Görevin arka planda işleneceğini ve sonucu tamamlanınca bildireceğini "
                "kısa, doğal Türkçe ile söyle; sonuç uydurma ve görevi ikinci kez ekleme."
            )
            return

        # bkz. dosyanin basindaki _is_github_self_improve_request notu -
        # modelin arac secimine birakmadan dogrudan agent_loop'a yonlendir.
        if _is_github_self_improve_request(text):
            try:
                result = agent_loop_tool(parameters={"action": "add", "goal": text})
            except Exception as e:
                result = f"Görev eklenirken hata oluştu: {e}"
            self.ui.write_log(f"[agent_loop] {result}")
            try:
                log_turn("jarvis", result)
            except Exception as e:
                print(f"[JARVIS] ⚠️ conversation_log (agent_loop short-circuit): {e}")
            notify = (
                f"[AGENT_LOOP_EKLENDI] Kullanıcının GitHub'da kendisi için yeni bir yetenek "
                f"aramasını istediği görev, arka plan görev listesine ZATEN eklendi (gerçek "
                f"sonuç: \"{result}\"). Bunu kullanıcıya kısaca doğal bir dille onayla - arka "
                f"planda arayacağını, bir şey bulursa entegre etmeden önce onayını isteyeceğini "
                f"söyle. Bunun için web_search, github_arama veya başka bir araç ÇAĞIRMA - "
                f"görev zaten eklendi, sadece bu sonucu bildir."
            )
            fut = asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"parts": [{"text": notify}]},
                    turn_complete=True,
                ),
                self._loop,
            )
            fut.add_done_callback(
                lambda f: self._log_send_result(f, "text_command:agent_loop_notify")
            )
            return

        fut = asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )
        fut.add_done_callback(lambda f: self._log_send_result(f, "text_command"))

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        fut = asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )
        fut.add_done_callback(lambda f: self._log_send_result(f, "speak"))

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(
            "LANGUAGE GUARD: The user interface language is Turkish. Unless the user "
            "clearly asks for another language, understand speech and answer only in "
            "natural Turkish. Never mix Turkish with Russian or another language. "
            "Ignore ambient noise markers such as <noise>; do not repeat a sentence."
        )
        parts.append(sys_prompt)
        parts.append(
            "FINAL LANGUAGE RULE: Answer this user only in natural Turkish. Do not answer "
            "in Russian, Telugu, or any other language merely because speech recognition "
            "produced a foreign-script fragment. Never repeat the same sentence twice."
        )

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            max_output_tokens=16384,
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Charon"
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror listing/info results to the on-screen content panel.
                # Sesli yanit genelde "listelendi efendim" gibi ozetleyip
                # gercek dosya adlarini hic soylemiyor - kullanici bunu fark
                # etti (2026-09-21). Diger arac sonuclari (web_search) icin
                # zaten yapilan show_content aynasi burada da yapiliyor ki
                # gercek icerik (dosya adlari) sesli ozetten bagimsiz olarak
                # gorunur olsun.
                _fc_action = str(args.get("action", "")).lower()
                if r and _fc_action in ("list", "find", "largest", "disk_usage", "info"):
                    _fc_path = args.get("path", "")
                    _fc_label = f"FILES — {_fc_action.upper()}" + (f" ({_fc_path})" if _fc_path else "")
                    self.ui.show_content(_fc_label, r)
                    # Ayrica log/gecmis paneline de yaz - kullanici sadece
                    # sesli/ozet yaniti degil, ham listeyi de metin olarak
                    # (kopyalayip yapistirabilecegi transcript'te) gorsun.
                    self.ui.write_log(f"[{_fc_label}]\n{r}")

            elif name == "task_manager":
                r = await loop.run_in_executor(None, lambda: task_manager(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "health_check":
                r = await loop.run_in_executor(None, lambda: health_check(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "start_parallel_task":
                r = await loop.run_in_executor(None, lambda: start_parallel_task(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "check_agent_board":
                r = await loop.run_in_executor(None, lambda: check_agent_board(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE natural sentence in the user's language "
                        f"(e.g. 'Looking at your {_stall} now, sir' / "
                        f"'{'Kameraya' if _stall == 'camera' else 'Ekrana'} bakıyorum efendim'). "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "self_improve":
                r = await loop.run_in_executor(None, lambda: self_improve(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "agent_loop":
                r = await loop.run_in_executor(None, lambda: agent_loop_tool(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "recall_conversation":
                r = await loop.run_in_executor(None, lambda: recall_conversation(args))
                result = r or "Done."

            elif name == "github_arama":
                r = await loop.run_in_executor(None, lambda: github_search(args))
                result = r or "Done."

            elif name == "discovered_topydo":
                r = await loop.run_in_executor(None, lambda: discovered_topydo_run(args))
                result = r or "Done."

            elif name == "discovered_jc":
                r = await loop.run_in_executor(None, lambda: discovered_jc_run(args))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "system_scan_and_repair":
                from jarvis.actions.system_scan import system_scan_and_repair
                r = await loop.run_in_executor(None, system_scan_and_repair)
                result = str(r)

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")
                def _shutdown():
                    import time
                    import os
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        import numpy as _np
        _level_state = {"last_print": 0.0}
        stream_rate = SEND_SAMPLE_RATE

        def _input_rate(device: int) -> int:
            """Choose a rate the Windows driver accepts; output stays Gemini 16 kHz."""
            try:
                sd.check_input_settings(
                    device=device, samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS, dtype="int16",
                )
                return SEND_SAMPLE_RATE
            except Exception:
                info = sd.query_devices(device)
                native = int(round(float(info.get("default_samplerate", 0))))
                if native <= 0 or native == SEND_SAMPLE_RATE:
                    raise
                sd.check_input_settings(
                    device=device, samplerate=native,
                    channels=CHANNELS, dtype="int16",
                )
                return native

        def callback(indata, frames, time_info, status):
            # TESHIS: ses seviyesini periyodik olarak ekrana yazdir - gercek
            # sinyal geliyor mu, yoksa mikrofon tamamen sessiz mi gorelim.
            now = time.monotonic()
            if now - _level_state["last_print"] > 2.0:
                _level_state["last_print"] = now
                peak = int(_np.abs(indata).max()) if indata.size else 0
                rms = float(_np.sqrt(_np.mean(_np.square(indata.astype(_np.float32))))) if indata.size else 0.0
                bar = "█" * min(50, peak // 200)
                print(f"[JARVIS] 🎚️ Mikrofon seviyesi: {peak:5d} {bar}")
                try:
                    self.ui.set_voice_volume(min(1.0, rms / 4500.0))
                except Exception:
                    pass

            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if not jarvis_speaking and not self.ui.muted and not self._phone_active:
                # Bazı Windows sürücülerinde otomatik gain/kulaklık yankısı
                # örnekleri 10k+ seviyesine taşıyor. Bu örnekleri olduğu gibi
                # göndermek Live transkripsiyonunu parçalıyor ve yanlış komut
                # üretiyor. Normal sinyale dokunma; yalnızca anormal peak'i
                # yaklaşık 3000'e yumuşak biçimde indir.
                _peak = int(_np.abs(indata).max()) if indata.size else 0
                if _peak > 6000:
                    _gain = 3000.0 / _peak
                    _safe = _np.clip(indata.astype(_np.float32) * _gain, -32768, 32767).astype(_np.int16)
                    samples = _safe
                    data = _safe.tobytes()
                else:
                    samples = indata
                if stream_rate != SEND_SAMPLE_RATE:
                    # Gemini Live expects mono signed-int16 PCM at 16 kHz.
                    # Windows devices commonly expose only 44.1/48 kHz;
                    # convert inside the callback instead of rejecting them.
                    mono = samples[:, 0] if samples.ndim > 1 else samples
                    out_len = max(1, round(len(mono) * SEND_SAMPLE_RATE / stream_rate))
                    positions = _np.linspace(0, len(mono) - 1, out_len)
                    samples = _np.interp(positions, _np.arange(len(mono)), mono).astype(_np.int16)
                data = samples.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm;rate=16000"}
                )

        # Cihaz secim mantigi actions/audio_devices.py'de tek yerde toplu -
        # health_check.py da ayni fonksiyonlari kullanir, ikisi birbirinden
        # sapmaz.
        #
        # ONEMLI: eskiden mikrofon hicbir cihazda acilamadiginda buradan
        # RuntimeError firlatiliyordu - bu da main.py'deki TaskGroup'u
        # coturuyor, boylece calisan HOPARLOR ve Gemini oturumu da dahil
        # HER SEY yikilip 3 saniyede bir bastan kuruluyordu. Mikrofon
        # surucusu kalici bir sorun yasiyorsa (ör. PortAudio'nun cihazi
        # "Invalid device" ile reddetmesi) bu sonsuz bir cokme dongusune
        # donusuyor ve "Jarvis hicbir seye baglanamiyor" izlenimi veriyordu.
        # Artik mikrofon acilamazsa SADECE mikrofon devre disi kalir; metin/
        # arac cagirilari ve hoparlor calismaya devam eder, mikrofon ise
        # arka planda periyodik olarak yeniden denenir.
        RETRY_DELAY = 10.0
        while True:
            if _mic_breaker.is_open():
                print("[JARVIS] ⏳ Mikrofon devre kesici açık, deneme atlanıyor.")
                try:
                    self.ui.set_mic_device("devre kesici açık - bekleniyor")
                except Exception:
                    pass
                await asyncio.sleep(RETRY_DELAY)
                continue

            # TEK bir anlik goruntu: adaylari bulma + kimlik yakalama + dogrulama
            # AYNI enumerasyon uzerinden yapilir. Bazi Bluetooth Hands-Free
            # cihazlari ismini sorgular arasinda tutarsiz raporluyor (bkz.
            # resolve_device_index docstring'i) - ayri sorgular kullanmak bu
            # cihazlarda mikrofonun hic acilamamasina yol aciyordu.
            _devices_snapshot = _list_audio_devices()
            candidates = _audio_candidates("input", devices=_devices_snapshot)
            print(f"[JARVIS] Mikrofon adaylari: {candidates}")

            last_err = None
            opened = False
            if not candidates:
                last_err = RuntimeError("no usable input device")
            for cand in candidates:
                resolved = _resolve_audio_device(
                    "input",
                    _audio_device_identity("input", cand, devices=_devices_snapshot),
                    devices=_devices_snapshot,
                )
                if resolved is None:
                    last_err = RuntimeError("input device changed or is ambiguous")
                    print(f"[JARVIS] ⚠️ Mikrofon adayi yeniden dogrulanamadi (device={cand})")
                    continue
                try:
                    stream_rate = _input_rate(resolved)
                    if stream_rate != SEND_SAMPLE_RATE:
                        print(f"[JARVIS] ℹ️ Mikrofon {stream_rate} Hz destekliyor; Gemini için 16 kHz'e dönüştürülüyor.")
                    with sd.InputStream(
                        device=resolved,
                        samplerate=stream_rate,
                        channels=CHANNELS,
                        dtype="int16",
                        blocksize=CHUNK_SIZE,
                        callback=callback,
                    ):
                        name = _audio_device_name(resolved, devices=_devices_snapshot)
                        print(f"[JARVIS] 🎤 Mic stream open (device={resolved} - {name})")
                        _mic_breaker.record_success()
                        opened = True
                        try:
                            self.ui.set_mic_device(name)
                        except Exception:
                            pass
                        while True:
                            await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    last_err = e
                    print(f"[JARVIS] ⚠️ Mikrofon acilamadi (device={resolved}): {type(e).__name__}")
                    continue

            if not opened:
                _mic_breaker.record_failure()
                err_label = type(last_err).__name__ if last_err else "bilinmiyor"
                print(f"[JARVIS] ❌ Mic: hiçbir giriş cihazı açılamadı ({last_err}) "
                      f"- {RETRY_DELAY:.0f}sn sonra tekrar denenecek")
                try:
                    self.ui.set_mic_device(f"YOK ({err_label})")
                except Exception:
                    pass
                await asyncio.sleep(RETRY_DELAY)
                continue

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    _audio_data = _response_audio_data(response)
                    if _audio_data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)
                                try:
                                    self.ui.set_voice_transcript(txt)
                                except Exception:
                                    pass

                        # LIVE_DIAG: Gemini Live response icinden transcript sinyalini izle.
                        try:
                            if sc.input_transcription:
                                print(f"[LIVE_DIAG] input_transcription objesi var: {sc.input_transcription!r}")
                            if getattr(sc, "input_transcription", None) is not None:
                                print(f"[LIVE_DIAG] input text: {getattr(sc.input_transcription, 'text', None)!r}")
                        except Exception as _diag_e:
                            print(f"[LIVE_DIAG] hata: {_diag_e}")

                        if sc.input_transcription and sc.input_transcription.text:

                            txt = _clean_transcript(sc.input_transcription.text)

                            if txt:

                                in_buf.append(txt)
                                try:
                                    self.ui.set_voice_state("USER_SPEAKING", "Canlı ses alınıyor")
                                    self.ui.set_voice_transcript(txt)
                                except Exception:
                                    pass

                                self._last_user_speech = time.monotonic()

                                # Turn complete gelmese bile dosya komutunu yakala.

                                try:

                                    _voice_candidate = " ".join(in_buf).strip()

                                    _voice_file_mod = match_file_modification(_voice_candidate)

                                    if _voice_file_mod:

                                        self._on_text_command(_voice_candidate)

                                        print('[VOICE_INPUT_ROUTER] Dosya komutu yakalandi ve route edildi.')

                                        try:

                                            self.ui.write_log('[VOICE_INPUT_ROUTER] Dosya komutu route edildi.')

                                        except Exception:

                                            pass

                                        in_buf = []

                                except Exception as e:

                                    print(f'[VOICE_INPUT_ROUTER] hata: {e}')

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                try:
                                    log_turn("user", full_in)
                                except Exception as e:
                                    print(f"[JARVIS] ⚠️ conversation_log (voice in): {e}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            # Sesli komutlardan dosya islemlerini deterministik router'a aktar.
                            try:
                                _voice_file_mod = match_file_modification(full_in)
                                if _voice_file_mod:
                                    self._on_text_command(full_in)
                                    print('[VOICE_FILE_ROUTER] Dosya komutu route edildi.')
                                    try:
                                        self.ui.write_log('[VOICE_FILE_ROUTER] Dosya komutu route edildi.')
                                    except Exception:
                                        pass
                            except Exception as e:
                                print(f'[VOICE_FILE_ROUTER] hata: {e}')
                                try:
                                    self.ui.write_log(f'[VOICE_FILE_ROUTER] hata: {e}')
                                except Exception:
                                    pass
                            in_buf = []

                            full_out = _dedupe_response(" ".join(out_buf).strip())
                            if full_out:
                                self.ui.write_log(f"Jarvis: {full_out}")
                                try:
                                    log_turn("jarvis", full_out)
                                except Exception as e:
                                    print(f"[JARVIS] ⚠️ conversation_log (jarvis out): {e}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            log_tool_call(fc.name)
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")
        _played_chunks = 0
        _last_audio_at = time.monotonic()

        # Ayni ortak modul (actions/audio_devices.py) - mic ile ayni EXCLUDE
        # listesini ve oncelik sirasini kullanir, health_check.py ile tutarli.
        while True:
            # Ayni "tek anlik goruntu" duzeltmesi mikrofon icin de gecerli -
            # bkz. yukarisi ve resolve_device_index docstring'i.
            _devices_snapshot = _list_audio_devices()
            out_candidates = _audio_candidates("output", devices=_devices_snapshot)
            print(f"[JARVIS] Hoparlor adaylari: {out_candidates}")
            stream = None
            last_out_err = None
            chosen_device = None
            if _speaker_breaker.is_open():
                print("[JARVIS] ⏳ Hoparlör devre kesici açık, deneme atlanıyor.")
            else:
                for cand in out_candidates:
                    resolved = _resolve_audio_device(
                        "output",
                        _audio_device_identity("output", cand, devices=_devices_snapshot),
                        devices=_devices_snapshot,
                    )
                    if resolved is None:
                        last_out_err = RuntimeError("output device changed or is ambiguous")
                        continue
                    try:
                        sd.check_output_settings(
                            device=resolved,
                            samplerate=RECEIVE_SAMPLE_RATE,
                            channels=CHANNELS,
                            dtype="int16",
                        )
                        trial = sd.RawOutputStream(
                            device=resolved,
                            samplerate=RECEIVE_SAMPLE_RATE,
                            channels=CHANNELS,
                            dtype="int16",
                            blocksize=CHUNK_SIZE,
                        )
                        trial.start()
                        stream = trial
                        chosen_device = resolved
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        last_out_err = e
                        print(f"[JARVIS] ⚠️ Hoparlor acilamadi (device={resolved}): {type(e).__name__}")
                        _close_audio_stream(locals().get("trial"))
                        continue

            if stream is None:
                _speaker_breaker.record_failure()
                err_label = type(last_out_err).__name__ if last_out_err else "no_device"
                print(f"[JARVIS] ❌ Play: kullanilabilir cikis yok ({err_label}); yeniden denenecek")
                try:
                    self.ui.set_speaker_device(f"YOK ({err_label})")
                except Exception:
                    pass
                await asyncio.sleep(10.0)
                continue

            _speaker_breaker.record_success()
            name = _audio_device_name(chosen_device, devices=_devices_snapshot)
            print(f"[JARVIS] 🔊 Hoparlor cihazi: {chosen_device} - {name}")
            try:
                self.ui.set_speaker_device(name)
            except Exception:
                pass

            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            self.audio_in_queue.get(),
                            timeout=0.1
                        )
                    except TimeoutError:
                        if (
                            self._turn_done_event
                            and self._turn_done_event.is_set()
                            and self.audio_in_queue.empty()
                        ):
                            self.set_speaking(False)
                            try:
                                self.ui.set_voice_volume(0.0)
                            except Exception:
                                pass
                            self._turn_done_event.clear()
                        continue
                    self.set_speaking(True)
                    try:
                        await asyncio.to_thread(stream.write, chunk)
                        _played_chunks += 1
                        _last_audio_at = time.monotonic()
                        try:
                            self.ui.set_voice_volume(_pcm_rms_level(chunk))
                            self.ui.set_voice_state("ASSISTANT_SPEAKING", "JARVIS yanıt veriyor")
                        except Exception:
                            pass
                        if _played_chunks == 1:
                            print("[AUDIO_DIAG] İlk Gemini ses paketi hoparlör stream'ine yazıldı.")
                        elif _played_chunks % 200 == 0:
                            print(f"[AUDIO_DIAG] Hoparlöre yazılan ses paketi: {_played_chunks}")
                    except asyncio.CancelledError:
                        raise
                    except RuntimeError:
                        return
                    except Exception as write_error:
                        print(f"[AUDIO_DIAG] Hoparlöre ses yazılamadı: {type(write_error).__name__}")
                        _speaker_breaker.record_failure()
                        break
            finally:
                self.set_speaking(False)
                _close_audio_stream(stream)

            # A write failure should not tear down Gemini/text mode. Re-enumerate
            # and try a fresh stream after a short delay.
            await asyncio.sleep(1.0)

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _guarded_startup_briefing(self) -> None:
        """Briefing hatası ana Live oturumunu ve bağlantıyı düşürmemeli."""
        try:
            await self._send_startup_briefing()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Briefing] Başlangıç özeti atlandı: {exc}")
            try:
                self.ui.write_log(f"SYS: Startup briefing skipped after error: {exc}")
            except Exception:
                pass

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing for instant perceived response:
          Phase 1 — immediate greeting (no tools, no fetch) → Jarvis speaks in <2s
          Phase 2 — news fetched in background, injected after greeting finishes
        """
        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── memory ───────────────────────────────────────────────────────────
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")

        from datetime import datetime
        time_str = datetime.now().strftime("%H:%M")

        # ── Phase 1: instant greeting — one simple sentence ──────────────────
        # Açılışta otomatik haber çağrısı; kota/rate-limit durumunda her
        # başlatmada aynı hatayı üretip kullanıcı komutlarını da geciktiriyordu.
        # İsteyen kullanıcı JARVIS_STARTUP_NEWS=1 ile eski davranışı açabilir.
        startup_news = os.environ.get("JARVIS_STARTUP_NEWS", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        lang_clause = f" Respond in {lang}." if lang else ""
        name_clause = f" Address the user as {name}." if name else ""
        if startup_news:
            p1 = (
                f"Greet the user, mention it is {time_str}, and say you are fetching today's news headlines now. "
                f"One short sentence only. Do not call any tools.{lang_clause}{name_clause}"
            )
        else:
            p1 = (
                f"Greet the user, mention it is {time_str}. One short sentence only. "
                f"Do not call any tools.{lang_clause}{name_clause}"
            )

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fetch news in background, deliver after greeting plays ───
        if not startup_news:
            self.ui.write_log("SYS: Startup news briefing skipped (JARVIS_STARTUP_NEWS=0).")
            return
        async def _guarded_news():
            try:
                await self._briefing_news_phase(lang)
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing news phase failed: {e}")
        asyncio.create_task(_guarded_news())

        # ── Phase 3: mention a repeated-request pattern, if one was found ─────
        async def _guarded_pattern():
            try:
                await self._briefing_pattern_phase(lang)
            except Exception as e:
                print(f"[Briefing] Phase 3 error: {e}")
        asyncio.create_task(_guarded_pattern())

    async def _briefing_news_phase(self, lang: str) -> None:
        """
        Sends phase-2 (news) to Gemini ~1.5 s after phase-1 is dispatched so
        Gemini starts working on it while phase-1 audio is still playing.
        """
        lang_str = f" Respond in {lang}." if lang else ""

        # 1.5 s is enough for Gemini to finish generating phase-1 audio on its
        # side (turn_complete) while the greeting is still being played locally.
        await asyncio.sleep(1.5)

        if not self.session:
            return

        p2 = (
            "[BRIEFING] Call web_search with mode='news' and query='Türkiye güncel haberler bugün' "
            "to find actual recent Turkish news articles with real event headlines (not just website names). "
            "After the search, say ONE specific news event from the results in one sentence, "
            f"then say the full list is displayed on screen.{lang_str}"
        )

        await self.session.send_client_content(
            turns={"parts": [{"text": p2}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 2 (news) sent.")

    async def _briefing_pattern_phase(self, lang: str) -> None:
        """
        If the user has repeated the same kind of request on several
        different days, mention it once, naturally, in the morning
        briefing - e.g. "I noticed you usually check the weather around
        9am, so I already have it ready." Sends nothing if no real
        pattern exists (avoids an annoying forced mention every day).
        """
        lang_str = f" Respond in {lang}." if lang else ""

        await asyncio.sleep(3.0)  # phase 1 ve phase 2'nin arkasından gelsin

        if not self.session:
            return

        patterns = detect_patterns(min_days=3)
        if not patterns:
            return

        top = patterns[0]
        p3 = (
            f"[BRIEFING] Kullanıcının tekrar eden bir alışkanlığını fark ettin: {top}. "
            "Bunu kullanıcıya TEK, kısa ve doğal bir cümleyle, yapmacık olmadan hatırlat "
            "(örn. 'Bu arada, genelde bu saatte ... sorduğunu fark ettim, hazırladım bile.'). "
            f"Uzun açıklama yapma, sadece tek cümle.{lang_str}"
        )

        await self.session.send_client_content(
            turns={"parts": [{"text": p3}]},
            turn_complete=True,
        )
        self.ui.write_log(f"SYS: Briefing phase 3 (pattern): {top}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if alert and self.session:
                try:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": alert}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_scheduled_tasks(self) -> None:
        """
        Background task: periodically checks for due scheduled commands
        (added via task_manager) and, when a task's time comes, sends its
        command text into the live session exactly like a real user turn —
        using the same safe mechanism as _run_proactive_mode.
        """
        while True:
            await asyncio.sleep(20)

            if not self.session:
                continue

            try:
                due_tasks = await asyncio.to_thread(pop_due_tasks)
            except Exception as e:
                print(f"[Automation] ⚠️ {e}")
                continue

            for task in due_tasks:
                try:
                    self.ui.write_log(f"SYS: ⏰ Zamanlanmış görev çalıştırılıyor: {task['command']}")
                    scheduled_prompt = (
                        f"[Zamanlanmış hatırlatma tetiklendi, kullanıcı şu an bunu söylemedi — "
                        f"sen daha önce bunu ayarlamıştın] Şimdi kullanıcıya, doğal ve sıcak bir "
                        f"cümleyle hatırlat: \"{task['command']}\". 'Komut çalıştırıldı' gibi robotik "
                        f"bir rapor verme; sanki kendiliğinden hatırlıyormuşsun gibi doğal konuş, "
                        f"gerekirse ilgili aracı da çağır."
                    )
                    await self.session.send_client_content(
                        turns={"parts": [{"text": scheduled_prompt}]},
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Automation] ⚠️ görev çalıştırılamadı ({task.get('id')}): {e}")

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory = await asyncio.to_thread(load_memory)
                prompt = self._proactive.build_prompt(memory)
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Dashboard/phone audio is opt-in. The dashboard module owns its own
        # firewall policy; this gate prevents importing/starting it by default.
        if os.environ.get("JARVIS_ENABLE_DASHBOARD", "0").strip() == "1":
            try:
                from jarvis.dashboard.server import DashboardServer
                self._dashboard = DashboardServer()
                self._dashboard.set_connect_callback(self._on_phone_connected)
                asyncio.create_task(self._dashboard.serve())
                asyncio.create_task(self._process_dashboard_commands())
            except Exception as e:
                print(f"[Dashboard] Disabled: {type(e).__name__}")
                self._dashboard = None
        else:
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("CONNECTING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[JARVIS] Connected.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")
                    # GECICI TANI SATIRI - hangi kod surumunun gercekten calistigini
                    # dogrulamak icin eklendi (github-fix-v3). Dogrulama sonrasi kaldirilabilir.
                    self.ui.write_log("SYS: [KOD-SÜRÜMÜ: github-fix-v3]")

                    # Arka plan gorev dongusu - kendi thread'inde calisir,
                    # bir kere baslatilir (start_background_loop guard'li),
                    # mic/hoparlor/Gemini oturumundan bagimsizdir.
                    try:
                        start_agent_loop()
                    except Exception as e:
                        print(f"[JARVIS] ⚠️ agent_loop başlatılamadı: {e}")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_scheduled_tasks())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch
                    if not self._briefing_sent:
                        self._briefing_sent = True
                        tg.create_task(self._guarded_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                auth_error = _is_auth_error(err_str)
                # Do not echo exception text or traceback: SDK errors can carry
                # request metadata. Authentication waits for user correction
                # instead of retrying a known-invalid key.
                if auth_error:
                    print("[JARVIS] Authentication requires user correction.")
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("AUTH_REQUIRED")
                    self.ui.prompt_reconfig()
                    win = getattr(self.ui, "_win", None)
                    while win is not None and not getattr(win, "_ready", False):
                        await asyncio.sleep(0.5)
                    self._conn_backoff = 3
                    continue

                print(f"[JARVIS] Error ({type(e).__name__}); reconnect will be delayed.")

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    # v25'te burada kökte olmayan bir "face.png" aranıyordu; HUD sessizce
    # yüzsüz açılıyordu. Artık pakete gömülü ikon kullanılıyor, JARVIS_FACE
    # ile kullanıcı kendi görselini verebiliyor.
    face = os.environ.get("JARVIS_FACE", "").strip() or str(asset("jarvis_icon.png"))
    ui = JarvisUI(face)

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()
