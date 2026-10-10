from __future__ import annotations

import math
import json
import os
import platform
import random
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QPointF, QRectF, Qt,
    QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from jarvis.core.secure_config import get_gemini_api_key, load_config, save_config, api_keys_path
from jarvis.paths import memory_dir, tasks_dir
from jarvis.network_discovery import (
    LocalIPv4Network, NetworkScopeError, ScanSnapshot,
    active_local_networks, parse_scope, scan_lan, service_labels,
    suggested_scope,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR = _base_dir()

_DEFAULT_W, _DEFAULT_H = 1440, 900
_MIN_W,     _MIN_H = 1120, 700
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    # Premium workstation palette — cyan is the primary navigation/action
    # colour, with violet reserved for secondary system information.
    BG        = "#030b16"
    PANEL     = "#07182b"
    PANEL2    = "#0a1d33"
    BORDER    = "#12395c"
    BORDER_B  = "#176b9b"
    BORDER_A  = "#124d75"
    PRI       = "#10bfff"
    PRI_DIM   = "#167caf"
    PRI_GHO   = "#062541"
    ACC       = "#10cbbf"
    ACC2      = "#8b6dff"
    GREEN     = "#11cfb2"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#d7edff"
    TEXT_DIM  = "#6ca6d2"
    TEXT_MED  = "#92c7ea"
    WHITE     = "#f2f8ff"
    DARK      = "#031225"
    BAR_BG    = "#071a31"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(1.5)

    def stop(self):
        self._running = False

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free, works on all platforms if installed
        try:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes
        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess)
        if _OS == "Windows":
            try:
                import wmi  # type: ignore
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


# Importing the UI must not start a telemetry worker.
_metrics: _SysMetrics | None = None

class HudCanvas(QWidget):
    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "BAŞLATILIYOR"

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.70
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.70))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.47, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # corner brackets
        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw // 2, cx + fw // 2
        ht, hb = cy - fw // 2, cy + fw // 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # face
        if self._face_px:
            fsz    = int(fw * 0.62 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            oc    = (200, 0, 50) if self.muted else (0, 60, 110)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, "J.A.R.V.I.S")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # status text
        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "⊘  SESSİZE ALINDI",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  KONUŞUYOR",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  DÜŞÜNÜYOR",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  İŞLENİYOR", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  DİNLİYOR",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            elif self.speaking:
                hgt = random.randint(3, 20)
                cl  = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl  = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "JARVIS için bir dosya seçin", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Dosyayı buraya sürükleyin  ya da  Gözat'a tıklayın")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cy = H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Yüklemek için bırakın")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CameraPreview")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            QWidget#CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("İlk açılıştan önce J.A.R.V.I.S.'i yapılandırın.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API ANAHTARI", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("İŞLETİM SİSTEMİ", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

        local_btn = QPushButton("CONTINUE IN LOCAL UI (NO API KEY)")
        local_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        local_btn.setFixedHeight(30)
        local_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        local_btn.setStyleSheet(f"QPushButton {{ background:transparent; color:{C.TEXT_DIM}; border:1px solid {C.BORDER}; border-radius:3px; }} QPushButton:hover {{ color:{C.TEXT}; border:1px solid {C.BORDER_B}; }}")
        local_btn.clicked.connect(self.dismissed.emit)
        layout.addWidget(local_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()
    _connected_sig = pyqtSignal()

    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url)

        lay.addWidget(_lbl("Anında bağlanmak için telefon kamerasıyla tarayın", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Ya da elle girin:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("YENİ ANAHTAR")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("KAPAT")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        # Dashboard callbacks run on the asyncio thread.  Never mutate Qt
        # widgets from that thread; queued signal delivery keeps the overlay
        # state consistent when the window is closing at the same time.
        self._connected_sig.connect(self._apply_connected)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Thread-safe entry point called when a phone successfully connects."""
        self._connected_sig.emit()

    def _apply_connected(self) -> None:
        """Update the overlay on the Qt GUI thread only."""
        self._ctimer.stop()
        self._key_lbl.setText("BAĞLANDI")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class CircularMetric(QWidget):
    def __init__(self, label: str, color: str, parent=None):
        super().__init__(parent)
        self.label, self.color, self.value, self.detail = label, color, 0.0, "N/A"
        self.setMinimumSize(96, 122)

    def set_value(self, value: float, detail: str = ""):
        self.value = max(0.0, min(100.0, float(value)))
        if detail: self.detail = detail
        self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, 42; r = 29
        p.setPen(QPen(qcol(C.BORDER), 5)); p.drawArc(QRectF(cx-r, cy-r, r*2, r*2), 0, 360*16)
        p.setPen(QPen(qcol(self.color), 5)); p.drawArc(QRectF(cx-r, cy-r, r*2, r*2), 90*16, int(-self.value*3.6*16))
        p.setPen(QPen(qcol(C.WHITE), 1)); p.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        p.drawText(QRectF(cx-r, cy-9, r*2, 20), Qt.AlignmentFlag.AlignCenter, f"{int(self.value)}%" if self.detail != "N/A" else "N/A")
        p.setPen(QPen(qcol(C.TEXT_MED), 1)); p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(0, 70, self.width(), 18), Qt.AlignmentFlag.AlignCenter, self.label)
        p.setPen(QPen(qcol(C.TEXT_DIM), 1)); p.setFont(QFont("Segoe UI", 7))
        p.drawText(QRectF(0, 88, self.width(), 16), Qt.AlignmentFlag.AlignCenter, self.detail)


class WaveformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._active = False
        self.setMinimumHeight(78)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(40)

    def set_active(self, active: bool):
        self._active = bool(active)
        self.update()

    def stop(self):
        self._timer.stop()

    def _animate(self):
        if self._active:
            self._phase += 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height(); mid = h / 2
        p.setPen(QPen(qcol(C.BORDER), 1)); p.drawLine(8, int(mid), max(8, w - 8), int(mid))
        if self._active:
            bars = 54; gap = max(2, w / (bars + 2))
            for i in range(bars):
                x = 12 + i * gap; envelope = max(0.08, 1 - abs(i - bars / 2) / (bars / 2))
                amp = 8 + 24 * envelope * (0.45 + 0.55 * abs(math.sin(self._phase + i * .72)))
                grad = QLinearGradient(0, mid - amp, 0, mid + amp); grad.setColorAt(0, qcol(C.ACC2)); grad.setColorAt(.5, qcol(C.PRI)); grad.setColorAt(1, qcol(C.ACC2))
                p.setPen(QPen(QBrush(grad), 3)); p.drawLine(QPointF(x, mid - amp), QPointF(x, mid + amp))
        p.setPen(QPen(qcol(C.TEXT_MED), 1)); p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(34, h - 16, max(160, w - 68), 14), "Canlı yanıt akışı" if self._active else "Backend yanıtı bekleniyor")


class VoiceHudWidget(QWidget):
    """Compact PyQt6 adaptation of the imported voice-assistant state HUD.

    It intentionally stays in the JARVIS Qt process so Tkinter and a second
    multiprocessing GUI event loop are not introduced into the production UI.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "LISTENING"
        self._detail = "Dinliyor"
        self._transcript = ""
        self._volume = 0.0
        self._phase = 0.0
        self.setMinimumWidth(238)
        self.setFixedHeight(42)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate)
        self._timer.start(40)

    def set_state(self, state: str, detail: str = ""):
        self._state = str(state or "ERROR").upper()
        if detail:
            self._detail = str(detail)
        self.update()

    def set_transcript(self, text: str):
        self._transcript = str(text or "")[-46:]
        self.update()

    def set_volume(self, value: float):
        try:
            self._volume = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            self._volume = 0.0
        self.update()

    def _animate(self):
        if self._state in {"USER_SPEAKING", "SPEAKING", "THINKING"}:
            self._phase += 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.drawRoundedRect(rect, 12, 12)

        colors = {
            "LISTENING": C.GREEN,
            "USER_SPEAKING": C.PRI,
            "SPEAKING": C.ACC2,
            "THINKING": C.ACC2,
            "SLEEPING": C.TEXT_DIM,
            "MUTED": C.MUTED_C,
            "ERROR": C.RED,
        }
        color = colors.get(self._state, C.PRI)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(qcol(color)))
        p.drawEllipse(QRectF(12, 16, 8, 8))

        title = {
            "LISTENING": "DİNLİYOR",
            "USER_SPEAKING": "KONUŞUYORSUNUZ",
            "SPEAKING": "JARVIS KONUŞUYOR",
            "THINKING": "DÜŞÜNÜYOR",
            "SLEEPING": "UYKU MODU",
            "MUTED": "MİKROFON KAPALI",
            "ERROR": "SES HATASI",
        }.get(self._state, self._state)
        p.setPen(QPen(qcol(C.WHITE), 1))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(QRectF(27, 6, 104, 14), Qt.AlignmentFlag.AlignLeft, title)

        detail = self._transcript if self._transcript and self._state in {"USER_SPEAKING", "SPEAKING"} else self._detail
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.setFont(QFont("Segoe UI", 7))
        p.drawText(QRectF(27, 21, 112, 13), Qt.AlignmentFlag.AlignLeft, detail[:24])

        mid = self.height() / 2
        for i in range(10):
            x = 151 + i * 7
            amp = 3 + 11 * max(self._volume, 0.18) * (0.45 + 0.55 * abs(math.sin(self._phase + i * 0.7)))
            p.setPen(QPen(qcol(C.PRI if i % 2 == 0 else C.ACC2), 2))
            p.drawLine(QPointF(x, mid - amp), QPointF(x, mid + amp))



class SystemNetworkWidget(QWidget):
    node_selected = pyqtSignal(str)

    WIDTH = 1000
    HEIGHT = 760
    NODES = (
        ("main", 500, 335, 54, "JARVIS", "core"),
        ("ui", 130, 150, 38, "Arayüz", "core"),
        ("voice", 105, 335, 40, "Ses + Live", "core"),
        ("memory", 135, 520, 40, "Hafıza", "memory"),
        ("history", 300, 625, 39, "Konuşma", "memory"),
        ("storage", 335, 150, 40, "Yerel veri", "support"),
        ("web", 820, 150, 42, "Web & arama", "actions"),
        ("desktop", 880, 310, 42, "Masaüstü", "actions"),
        ("files", 820, 475, 42, "Dosya & kod", "actions"),
        ("tasks", 700, 565, 42, "Görevler", "actions"),
        ("system", 505, 565, 42, "Sistem", "support"),
        ("integrations", 930, 535, 42, "Bağlantılar", "actions"),
        ("brain", 500, 440, 48, "Beyin takımı", "brain"),
        ("brain_support", 350, 510, 30, "Takım altyapısı", "support"),
        ("planner", 115, 690, 26, "Planner", "brain"),
        ("research", 235, 690, 26, "Research", "brain"),
        ("coder", 355, 690, 26, "Coder", "brain"),
        ("security", 475, 690, 26, "Security", "brain"),
        ("brain_memory", 595, 690, 26, "Memory AI", "brain"),
        ("executor", 715, 690, 26, "Executor", "brain"),
        ("auditor", 835, 690, 26, "Auditor", "brain"),
    )
    EDGES = (
        ("main", "ui"), ("main", "voice"), ("main", "memory"), ("main", "history"),
        ("main", "storage"), ("main", "web"), ("main", "desktop"), ("main", "files"),
        ("main", "tasks"), ("main", "system"), ("main", "integrations"), ("main", "brain"),
        ("ui", "storage"), ("memory", "storage"), ("tasks", "storage"),
        ("brain", "brain_support"), ("brain", "planner"), ("brain", "research"),
        ("brain", "coder"), ("brain", "security"), ("brain", "brain_memory"),
        ("brain", "executor"), ("brain", "auditor"),
    )
    DETAILS = {
        "main": ("src/jarvis/main.py", "Canlı sesli oturumun ve araç çağrılarının ana giriş noktası."),
        "ui": ("src/jarvis/ui.py", "JarvisUI penceresi; konuşma, görev ve sistem sayfalarını oluşturur."),
        "voice": ("src/jarvis/main.py · google.genai · sounddevice", "Gemini Live oturumu ve ses giriş/çıkış akışı."),
        "memory": ("src/jarvis/memory/memory_manager.py · sanitizer.py", "Konuşma bağlamını okur ve kullanıcı hafızasını günceller."),
        "history": ("src/jarvis/actions/conversation_log.py", "Gerçek kullanıcı ve Jarvis konuşma turlarını kaydeder ve geri çağırır."),
        "storage": ("src/jarvis/paths.py · core/secure_config.py · memory/sanitizer.py", "Yerel veri yolları, güvenli kullanıcı ayarları ve hafıza temizliği."),
        "web": ("src/jarvis/actions/browser_control.py · web_search.py · github_arama.py", "main.py içindeki araç yönlendiricisinin web ve arama modülleri."),
        "desktop": ("src/jarvis/actions/open_app.py · desktop.py · computer_control.py · computer_settings.py · powershell_control.py", "Uygulama, masaüstü ve açıkça istenen komut işlemleri."),
        "files": ("src/jarvis/actions/file_controller.py · file_processor.py · code_helper.py · dev_agent.py · self_improve.py", "Dosya ve geliştirme araçları; main.py tarafından yönlendirilir."),
        "tasks": ("src/jarvis/actions/agent_board.py · agent_loop.py · automation.py · reminder.py", "Görev panosu, arka plan agent döngüsü, otomasyon ve hatırlatıcılar."),
        "system": ("src/jarvis/actions/system_monitor.py · health_check.py · audio_devices.py · resilience.py · proactive.py · pattern_tracker.py", "Sistem sağlığı ve dayanıklılık araçları."),
        "integrations": ("src/jarvis/actions/send_message.py · weather_report.py · flight_finder.py · youtube_video.py · game_updater.py · discovered_topydo.py · discovered_jc.py", "Mesaj, hava, uçuş, medya ve keşfedilmiş araç bağlantıları."),
        "brain": ("src/jarvis/core/brain_orchestrator.py", "Canlı oturum açılınca gerçek arka plan görev döngüsü başlar; uzman beyinlerin durumları heartbeat ile izlenir."),
        "brain_support": ("src/jarvis/core/message_bus.py · task_manager.py · watchdog.py · src/jarvis/actions/capability_resolver.py", "Beyin takımının içe aktardığı görev, mesaj ve izleme altyapısı."),
        "planner": ("src/jarvis/brains/planner_ai.py", "Beyin takımında görev adımlarını planlar."),
        "research": ("src/jarvis/brains/research_ai.py", "Beyin takımında araştırma adımlarını yürütür."),
        "coder": ("src/jarvis/brains/coder_ai.py", "Beyin takımının kod üretme ve inceleme uzmanı."),
        "security": ("src/jarvis/brains/security_ai.py", "Beyin takımında risk denetimine katılır."),
        "brain_memory": ("src/jarvis/brains/memory_ai.py", "Beyin takımının görev hafızası uzmanı."),
        "executor": ("src/jarvis/brains/executor_ai.py", "Beyin takımında yapılandırılmış eylemleri yürütür."),
        "auditor": ("src/jarvis/brains/auditor_ai.py", "Beyin takımında sonuçları denetler."),
    }
    TOOL_GROUPS = {
        "browser_control": "web", "web_search": "web", "github_arama": "web",
        "screen_process": "desktop", "close_camera": "desktop",
        "open_app": "desktop", "desktop_control": "desktop", "computer_control": "desktop",
        "computer_settings": "desktop", "powershell_control": "desktop",
        "file_controller": "files", "file_processor": "files", "code_helper": "files",
        "dev_agent": "files", "self_improve": "files",
        "task_manager": "tasks", "start_parallel_task": "tasks", "check_agent_board": "tasks",
        "agent_loop": "tasks", "reminder": "tasks",
        "health_check": "system", "system_status": "system", "system_scan_and_repair": "system",
        "shutdown_jarvis": "main",
        "send_message": "integrations", "weather_report": "integrations", "flight_finder": "integrations",
        "youtube_video": "integrations", "game_updater": "integrations",
        "discovered_topydo": "integrations", "discovered_jc": "integrations",
        "save_memory": "memory", "forget_memory": "memory", "recall_conversation": "history",
    }
    STATUS_LABELS = {
        "unknown": "Veri yok", "visible": "Hazır",
        "ready": "Hazır", "idle": "Boşta",
        "connecting": "Bağlanıyor", "connected": "Bağlı",
        "disconnected": "Bağlantı yok", "auth_required": "Kimlik doğrulaması gerekli",
        "running": "Çalışıyor", "returned": "Yanıt döndü",
        "observed": "Gözlendi", "pending": "Beklemede",
        "awaiting_approval": "Onay bekliyor", "waiting_approval": "Onay bekliyor",
        "completed": "Tamamlandı", "done": "Tamamlandı",
        "failed": "Başarısız", "cancelled": "İptal edildi", "error": "Hata",
        "recovery_required": "Recovery gerekli", "stale": "Heartbeat stale",
    }
    GROUP_COLORS = {
        "core": C.PRI, "memory": C.ACC2, "actions": C.ACC,
        "brain": C.MUTED_C, "support": "#ffb44a",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        # Let the stacked layout shrink this canvas to its real viewport.
        # A larger child minimum than the stack height caused the lower node
        # row to be painted underneath the fixed details/chat panels.
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.selected_node = "main"
        self._runtime = {"ui": ("visible", "Jarvis arayüzü açık")}
        self._runtime_received_at: dict[str, float] = {}
        self._task_records: list[dict] = []
        self._task_records_observed = False
        self._positions = {node[0]: (node[1], node[2], node[3]) for node in self.NODES}

    def _transform(self):
        width = max(1, self.width() - 12)
        height = max(1, self.height() - 12)
        scale = min(width / self.WIDTH, height / self.HEIGHT)
        return scale, (self.width() - self.WIDTH * scale) / 2, (self.height() - self.HEIGHT * scale) / 2

    def _node_at(self, point):
        scale, ox, oy = self._transform()
        x, y = (point.x() - ox) / scale, (point.y() - oy) / scale
        for node in reversed(self.NODES):
            node_id, nx, ny, radius = node[:4]
            if math.hypot(x - nx, y - ny) <= radius + 8:
                return node_id
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            node_id = self._node_at(event.position())
            if node_id:
                self.selected_node = node_id
                self.node_selected.emit(node_id)
                self.update()
        super().mousePressEvent(event)

    def update_runtime_status(self, component: str, status: str, detail: str = ""):
        component = str(component)
        state = str(status or "unknown").strip().lower()
        self._runtime[component] = (state, str(detail or "")[:180])
        self._runtime_received_at[component] = time.monotonic()
        self.update()

    def seconds_since_update(self, component: str) -> int | None:
        received_at = self._runtime_received_at.get(str(component))
        if received_at is None:
            return None
        return max(0, int(time.monotonic() - received_at))

    def update_tool_status(self, tool_name: str, status: str):
        group = self.TOOL_GROUPS.get(str(tool_name or ""))
        if group:
            self.update_runtime_status(group, status, str(tool_name))

    @staticmethod
    def _task_state(task: dict) -> str:
        status = str(task.get("status", "") or "").strip().lower()
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        recovery = task.get("_restart_recovery") or payload.get("_restart_recovery")
        if isinstance(recovery, dict) and recovery.get("action") == "manual_review":
            return "recovery_required"
        if status in {"awaiting_approval", "waiting_approval"}:
            return "awaiting_approval"
        if status in {"done", "completed", "failed", "cancelled", "pending", "running"}:
            return status
        return "observed"

    def update_task_records(self, tasks: list[dict]):
        self._task_records = [item for item in tasks if isinstance(item, dict)]
        self._task_records_observed = True
        if not self._task_records:
            self.update_runtime_status("tasks", "ready", "Kayıtlı aktif görev yok")
            return
        active = next((task for task in self._task_records if task.get("status") in
                       {"pending", "running", "awaiting_approval", "waiting_approval"}), None)
        recovery = next((task for task in self._task_records if self._task_state(task) == "recovery_required"), None)
        latest = recovery or active or self._task_records[0]
        goal = str(latest.get("goal", latest.get("name", latest.get("description", ""))))[:72]
        self.update_runtime_status(
            "tasks", self._task_state(latest),
            f"{latest.get('id', '—')} · {goal}".strip(" ·"),
        )

    def task_panel_details(self) -> tuple[str, str, str]:
        if not self._task_records_observed:
            return "Veri yok", "Veri yok", "Veri yok"
        recovery_task = next((task for task in self._task_records if self._task_state(task) == "recovery_required"), None)
        active = next((task for task in self._task_records if task.get("status") in
                       {"pending", "running", "awaiting_approval", "waiting_approval"}), None)
        if recovery_task:
            goal = str(recovery_task.get("goal", recovery_task.get("name", recovery_task.get("description", "Görev"))))[:90]
            active_text = f"Recovery gerekli · {goal}"
        elif active:
            state = self.STATUS_LABELS.get(str(active.get("status", "")).lower(), "Beklemede")
            goal = str(active.get("goal", active.get("name", active.get("description", "Görev"))))[:90]
            active_text = f"{state} · {goal}"
        else:
            active_text = "Yok · kayıtlı görevlerde aktif adım gözlenmedi"

        recovery_task = next((task for task in self._task_records if self._task_state(task) == "recovery_required"), None)
        if recovery_task:
            payload = recovery_task.get("payload") if isinstance(recovery_task.get("payload"), dict) else {}
            recovery = recovery_task.get("_restart_recovery") or payload.get("_restart_recovery") or {}
            recovery_text = str(recovery.get("reason") or recovery_task.get("id") or "İnceleme gerekli")[:110]
        else:
            recovery_text = "Kayıtlı görevlerde recovery işareti gözlenmedi"

        agent_error = next(
            (
                (node, value[1])
                for node in ("planner", "research", "coder", "security", "brain_memory", "executor", "auditor")
                if (value := self._runtime.get(node, ("unknown", "")))[0] in {"error", "failed"}
            ),
            None,
        )
        failed = next((task for task in self._task_records if str(task.get("status", "")).lower() in {"failed", "error"}), None)
        if agent_error:
            node, detail = agent_error
            error_text = detail or f"{node} son sağlık sinyalinde hata bildirdi"
        elif failed:
            error = failed.get("error") or failed.get("error_message") or failed.get("failure_reason")
            error_text = str(error)[:110] if error else f"Görev başarısız kaydedildi · {failed.get('id', '—')}"
        else:
            error_text = "Kayıtlı görev hatası gözlenmedi"
        return active_text, error_text, recovery_text

    def status_for_node(self, node_id: str):
        if node_id == "voice":
            observed = [(key, self._runtime[key]) for key in ("session", "audio", "audio_output") if key in self._runtime]
            if not observed:
                return "unknown", ""
            states = [state for _, (state, _) in observed]
            state = "error" if "error" in states else ("auth_required" if "auth_required" in states else
                    "connecting" if "connecting" in states else "disconnected" if all(s == "disconnected" for s in states) else
                    "connected" if any(s == "connected" for s in states) else "unknown")
            detail = " · ".join(f"{key}: {self.STATUS_LABELS.get(value[0], value[0])}" for key, value in observed)
            details = [value[1] for _, value in observed if value[1]]
            if details:
                detail += " · " + " · ".join(details)
            return state, detail
        state, detail = self._runtime.get(node_id, ("unknown", ""))
        if node_id in {"brain", "planner", "research", "coder", "security", "brain_memory", "executor", "auditor"} and state != "unknown":
            age = self.seconds_since_update(node_id)
            if age is not None and age >= 10:
                return "stale", f"Son durum sinyali UI'ye {age} sn önce ulaştı"
        return state, detail

    def status_text(self, node_id: str) -> str:
        state, detail = self.status_for_node(node_id)
        label = self.STATUS_LABELS.get(state, "Veri yok")
        return f"{label} · {detail}" if detail else label

    def _status_color(self, node_id: str) -> QColor:
        state, _ = self.status_for_node(node_id)
        colors = {
            "visible": C.GREEN, "ready": C.GREEN, "idle": C.GREEN,
            "connected": C.PRI, "returned": C.GREEN, "completed": C.GREEN,
            "running": C.PRI, "connecting": C.PRI, "pending": "#ffb44a",
            "awaiting_approval": "#ffb44a", "waiting_approval": "#ffb44a",
            "recovery_required": C.RED, "error": C.RED, "failed": C.RED,
            "auth_required": C.RED, "disconnected": C.TEXT_DIM,
            "stale": C.ACC2, "unknown": C.TEXT_DIM,
        }
        return QColor(colors.get(state, C.TEXT_DIM))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale, ox, oy = self._transform()
        painter.translate(ox, oy)
        painter.scale(scale, scale)

        grid_pen = QPen(QColor("#0b2537"), 0.7)
        painter.setPen(grid_pen)
        for x in range(20, self.WIDTH, 30):
            painter.drawLine(QPointF(x, 0), QPointF(x, self.HEIGHT))
        for y in range(20, self.HEIGHT, 30):
            painter.drawLine(QPointF(0, y), QPointF(self.WIDTH, y))

        positions = self._positions
        for source, target in self.EDGES:
            sx, sy, _ = positions[source]
            tx, ty, _ = positions[target]
            connected = self.selected_node in {source, target}
            color = QColor(C.PRI if connected else "#2b5973")
            color.setAlpha(235 if connected else 135)
            pen = QPen(color, 2.2 if connected else 1.2)
            if source == "main" and target == "brain":
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(sx, sy), QPointF(tx, ty))

        for node_id, x, y, radius, label, kind in self.NODES:
            color = QColor(self.GROUP_COLORS[kind])
            selected = node_id == self.selected_node
            if selected:
                halo = QColor(color)
                halo.setAlpha(42)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(halo))
                painter.drawEllipse(QPointF(x, y), radius + 12, radius + 12)
            outer = QColor("#0a1b2b")
            outer.setAlpha(245)
            painter.setPen(QPen(color if selected else color.darker(145), 2.0 if selected else 1.3))
            painter.setBrush(QBrush(outer))
            painter.drawEllipse(QPointF(x, y), radius + 5, radius + 5)
            core = QColor(C.PANEL2)
            core.setAlpha(255)
            painter.setPen(QPen(color, 2.0))
            painter.setBrush(QBrush(core))
            painter.drawEllipse(QPointF(x, y), radius - 3, radius - 3)

            state_color = self._status_color(node_id)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(state_color))
            painter.drawEllipse(QPointF(x + radius - 1, y - radius + 1), 4.5, 4.5)

            if node_id == "main":
                painter.setPen(QColor(C.WHITE))
                painter.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
                painter.drawText(QRectF(x - 48, y - 12, 96, 22), Qt.AlignmentFlag.AlignCenter, "JARVIS")
                painter.setPen(QColor(C.TEXT_DIM))
                painter.setFont(QFont("Segoe UI", 9))
                painter.drawText(QRectF(x - 48, y + 9, 96, 17), Qt.AlignmentFlag.AlignCenter, "main.py")
            elif node_id in {"planner", "research", "coder", "security", "brain_memory", "executor", "auditor"}:
                painter.setPen(QColor(C.TEXT))
                painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
                painter.drawText(QRectF(x - 52, y + radius + 3, 104, 17), Qt.AlignmentFlag.AlignCenter, label)
            else:
                painter.setPen(QColor(C.TEXT))
                painter.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
                painter.drawText(QRectF(x - 75, y + radius + 3, 150, 19), Qt.AlignmentFlag.AlignCenter, label)

        painter.end()


class LanNetworkMapWidget(QWidget):
    """Live map of observations from one explicitly selected local IPv4 subnet."""

    node_selected = pyqtSignal(str)
    WIDTH = 1000
    HEIGHT = 760

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.snapshot: ScanSnapshot | None = None
        self.local_address: str | None = None
        self.selected_node: str | None = None

    def set_local_address(self, address: str | None):
        self.local_address = str(address) if address else None
        self.update()

    def set_snapshot(self, snapshot: ScanSnapshot | None):
        self.snapshot = snapshot
        observations = sorted(
            snapshot.hosts if snapshot is not None else (),
            key=lambda item: int(item.address),
        )
        self.selected_node = str(observations[0].address) if observations else None
        self.update()

    def _transform(self):
        width = max(1, self.width() - 12)
        height = max(1, self.height() - 12)
        scale = min(width / self.WIDTH, height / self.HEIGHT)
        return scale, (self.width() - self.WIDTH * scale) / 2, (self.height() - self.HEIGHT * scale) / 2

    def _nodes(self):
        snapshot = self.snapshot
        if snapshot is None or not snapshot.hosts:
            return []

        observations = sorted(snapshot.hosts, key=lambda item: int(item.address))
        if len(observations) <= 24:
            groups = [observations]
            radii = [235]
        else:
            capacities = (16, 24, 40, 56, 64, 54)
            radii = (180, 220, 260, 300, 330, 355)
            groups = []
            offset = 0
            for capacity in capacities:
                if offset >= len(observations):
                    break
                groups.append(observations[offset:offset + capacity])
                offset += capacity
            if offset < len(observations):
                groups.append(observations[offset:])
                radii = (*radii, 385)

        nodes = []
        for group, radius in zip(groups, radii, strict=True):
            count = len(group)
            for index, observation in enumerate(group):
                if len(observations) == 1:
                    x, y = 500, 350
                else:
                    angle = -math.pi / 2 + (2 * math.pi * index / count)
                    x = 500 + radius * math.cos(angle)
                    y = 350 + radius * math.sin(angle)
                color = "#72e3bb" if observation.open_ports else (
                    "#61cce5" if "icmp" in observation.evidence else "#b89cff"
                )
                label = f".{str(observation.address).rsplit('.', 1)[-1]}"
                nodes.append((str(observation.address), x, y, 13, label, color))
        return nodes

    def _empty_state_text(self):
        if self.snapshot is None:
            return "Henüz tarama yapılmadı; haritada yalnızca yanıt veren cihazlar görünür."
        if self.snapshot.cancelled:
            return "Tarama durduruldu; bu taramada yanıt veren cihaz bulunmadı."
        return "Bu taramada yanıt veren cihaz bulunmadı."

    def _node_at(self, point):
        scale, ox, oy = self._transform()
        x, y = (point.x() - ox) / scale, (point.y() - oy) / scale
        for node in reversed(self._nodes()):
            if math.hypot(x - node[1], y - node[2]) <= node[3] + 8:
                return node[0]
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            node_id = self._node_at(event.position())
            if node_id:
                self.selected_node = node_id
                self.node_selected.emit(node_id)
                self.update()
        super().mousePressEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale, ox, oy = self._transform()
        painter.translate(ox, oy)
        painter.scale(scale, scale)

        painter.fillRect(0, 0, self.WIDTH, self.HEIGHT, QColor("#050b13"))
        grid_pen = QPen(QColor("#0b2537"), 0.7)
        painter.setPen(grid_pen)
        for x in range(20, self.WIDTH, 30):
            painter.drawLine(QPointF(x, 0), QPointF(x, self.HEIGHT))
        for y in range(20, self.HEIGHT, 30):
            painter.drawLine(QPointF(0, y), QPointF(self.WIDTH, y))

        nodes = self._nodes()
        if not nodes:
            painter.setPen(QColor(C.TEXT_DIM))
            painter.setFont(QFont("Segoe UI", 12))
            painter.drawText(
                QRectF(80, 320, 840, 50),
                Qt.AlignmentFlag.AlignCenter,
                self._empty_state_text(),
            )
            painter.end()
            return

        for node_id, x, y, radius, label, color_hex in nodes:
            color = QColor(color_hex)
            selected = node_id == self.selected_node
            if selected:
                halo = QColor(color)
                halo.setAlpha(48)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(halo))
                painter.drawEllipse(QPointF(x, y), radius + 13, radius + 13)

            outer = QColor("#0a1b2b")
            outer.setAlpha(245)
            painter.setPen(QPen(color.darker(145), 1.5))
            painter.setBrush(QBrush(outer))
            painter.drawEllipse(QPointF(x, y), radius + 4, radius + 4)
            painter.setPen(QPen(color, 1.7))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(x, y), max(4, radius - 3), max(4, radius - 3))

            painter.setPen(QColor(C.WHITE if selected or radius > 15 else C.TEXT))
            painter.setFont(QFont("Segoe UI", 9 if radius > 15 else 8, QFont.Weight.DemiBold))
            text_width = 130 if radius > 15 else 48
            painter.drawText(
                QRectF(x - text_width / 2, y + radius + 5, text_width, 17),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

        painter.end()


class LanScanWorker(QThread):
    result_ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int)

    def __init__(self, cidr: str, networks: tuple[LocalIPv4Network, ...], parent=None):
        super().__init__(parent)
        self.cidr = cidr
        self.networks = networks

    def run(self):
        try:
            result = scan_lan(
                self.cidr,
                local_networks=self.networks,
                cancelled=self.isInterruptionRequested,
                progress=self._report_progress,
            )
        except Exception as exc:
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.result_ready.emit(result)

    def _report_progress(self, completed: int, total: int):
        if completed == total or completed % 8 == 0:
            self.progress.emit(completed, total)

class MainWindow(QMainWindow):
    _log_sig     = pyqtSignal(str)
    _state_sig   = pyqtSignal(str)
    _voice_state_sig = pyqtSignal(str, str)
    _voice_transcript_sig = pyqtSignal(str)
    _voice_volume_sig = pyqtSignal(float)
    _content_sig = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig = pyqtSignal()          # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)   # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)   # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)  # live camera frame → HUD area
    _mic_dev_sig     = pyqtSignal(str)    # active microphone device name → left panel
    _speaker_dev_sig = pyqtSignal(str)    # active speaker/output device name → left panel
    _network_status_sig = pyqtSignal(str, str, str)

    def __init__(self, face_path: str, *, backend_enabled: bool = True):
        super().__init__()
        self._face_path = face_path
        self._backend_enabled = bool(backend_enabled)
        self.setWindowTitle("J.A.R.V.I.S — MuratJarvis")
        primary_screen = QApplication.primaryScreen()
        available = primary_screen.availableGeometry() if primary_screen else None
        available_w = available.width() if available else _DEFAULT_W
        available_h = available.height() if available else _DEFAULT_H
        window_w = min(_DEFAULT_W, available_w)
        window_h = min(_DEFAULT_H, available_h)
        self._left_panel_width = min(270, max(180, int(window_w * 0.19)))
        self._right_panel_width = min(380, max(250, int(window_w * 0.27)))
        self.setMinimumSize(min(_MIN_W, window_w), min(_MIN_H, window_h))
        self.resize(window_w, window_h)
        if available:
            self.move(
                available.x() + (available_w - window_w) // 2,
                available.y() + (available_h - window_h) // 2,
            )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self._muted            = False
        self._connection_state = "UNKNOWN"
        self._mic_device_name: str | None = None
        self._speaker_device_name: str | None = None
        self._status_updated_at: dict[str, datetime | None] = {
            "system": None, "gemini": None, "microphone": None, "speaker": None,
        }
        self._last_tool_context = ""
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None

          # ============================================================

        # ============================================================
        # J.A.R.V.I.S COMMAND CENTER V4
        # VISUAL LAYOUT ONLY
        # Existing backend objects/signals are preserved.
        # ============================================================

        central = QWidget()
        central.setStyleSheet(f"background:{C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0)
        self._left_panel = self._build_left_panel(); body.addWidget(self._left_panel)

        # Premium conversational command center. The HUD remains available for camera mode,
        # but the default view now follows the supplied product direction: messages first,
        # task execution second, command composer last.
        center = QWidget(); center.setStyleSheet(f"background:{C.BG};")
        cv = QVBoxLayout(center); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(0)
        cv.addWidget(self._build_chat_header())
        self._chat_stack = QStackedWidget()
        self._chat_panel = self._build_chat_panel()
        self._content_panel = self._build_content_panel()
        self._content_panel.hide()
        self._chat_panel.layout().addWidget(self._content_panel)
        self._chat_stack.addWidget(self._chat_panel)
        self.hud = HudCanvas(face_path); self.hud.hide()
        self._chat_stack.addWidget(self.hud)
        self._camera_page = QFrame(); self._camera_page.setObjectName("CameraPage")
        self._camera_page.setStyleSheet(f"QFrame#CameraPage {{ background:{C.BG}; border:none; }}")
        camera_layout = QVBoxLayout(self._camera_page); camera_layout.setContentsMargins(12, 12, 12, 12)
        self._cam_live_lbl = QLabel("Canlı kamera akışı bekleniyor"); self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter); self._cam_live_lbl.setMinimumSize(200, 160)
        self._cam_live_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:#02070c; border:1px solid {C.BORDER};")
        camera_layout.addWidget(self._cam_live_lbl, stretch=1); self._chat_stack.addWidget(self._camera_page)
        self._camera_page_index = self._chat_stack.indexOf(self._camera_page); self._hud_cam_stack = self._chat_stack
        self._tasks_page = self._build_tasks_center_page()
        self._team_page = self._build_team_center_page()
        self._settings_page = self._build_info_page(
            "Ayarlar", "Yerel cihaz ve bağlantı ayarları",
            [("GEMINI", "Anahtar varlığı ve uzak doğrulama durumu"),
             ("MİKROFON", "Sol paneldeki cihaz durumu kullanılıyor"),
             ("HOPARLÖR", "Sol paneldeki cihaz durumu kullanılıyor"),
             ("DASHBOARD", "Varsayılan: kapalı")],
            button_text="API AYARLARINI AÇ",
            button_slot=self._show_setup,
        )
        self._growth_page = self._build_growth_page()
        self._tasks_page_index = self._chat_stack.addWidget(self._tasks_page)
        self._team_page_index = self._chat_stack.addWidget(self._team_page)
        self._settings_page_index = self._chat_stack.addWidget(self._settings_page)
        self._growth_page_index = self._chat_stack.addWidget(self._growth_page)
        cv.addWidget(self._chat_stack, stretch=1); self._task_flow = self._build_task_flow(); cv.addWidget(self._task_flow)
        response = QFrame(); response.setObjectName("ResponseBar"); response.setFixedHeight(96)
        response.setStyleSheet(f"QFrame#ResponseBar {{ background:{C.PANEL}; border-top:1px solid {C.BORDER_B}; }}")
        rv = QHBoxLayout(response); rv.setContentsMargins(14, 8, 14, 8); rv.addWidget(self._build_input_row(), stretch=1)
        cv.addWidget(response)
        body.addWidget(center, stretch=1)
        self._right_panel = self._build_right_panel(); body.addWidget(self._right_panel)
        root.addLayout(body, stretch=1)

        # FOOTER
        root.addWidget(self._build_footer())


        self._clock_tmr = QTimer(self)

        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metrik güncelleme timer'ı
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()
        self._task_tmr = QTimer(self)
        self._task_tmr.timeout.connect(self._refresh_task_center)
        self._task_tmr.start(1500)
        self._refresh_task_center()

        self._log_sig.connect(self._on_log)
        self._state_sig.connect(self._apply_state)
        self._voice_state_sig.connect(self._apply_voice_state)
        self._voice_transcript_sig.connect(self._apply_voice_transcript)
        self._voice_volume_sig.connect(self._apply_voice_volume)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._mic_dev_sig.connect(self._on_mic_device)
        self._speaker_dev_sig.connect(self._on_speaker_device)
        self._network_status_sig.connect(self._apply_network_status)
        self._cam_stop = threading.Event()
        self._cam_thread: threading.Thread | None = None

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())
        self._cam_preview.hide()

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        self._network_initializing = True
        initial_state = (
            ("CONNECTING" if self._ready else "AUTH_REQUIRED")
            if self._backend_enabled
            else "LOCAL_ONLY"
        )
        self._apply_state(initial_state)
        self._network_initializing = False
        if not self._ready and self._backend_enabled:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)
        sc_remote = QShortcut(QKeySequence("F6"), self)
        sc_remote.activated.connect(self._open_remote)

    def _pill(self, text: str, color: str = C.PRI) -> QLabel:
        w = QLabel(text); w.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        w.setStyleSheet(f"color:{color}; background:{C.PRI_GHO}; border:1px solid {C.BORDER}; border-radius:10px; padding:5px 10px;")
        return w

    def _build_chat_header(self):
        bar = QFrame(); bar.setObjectName("ChatHeader"); bar.setFixedHeight(66)
        bar.setStyleSheet(f"QFrame#ChatHeader {{ background:{C.PANEL}; border-bottom:1px solid {C.BORDER}; }}")
        lay = QHBoxLayout(bar); lay.setContentsMargins(18, 10, 18, 10); lay.setSpacing(10)
        icon = QLabel("◈"); icon.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold)); icon.setStyleSheet(f"color:{C.PRI}; background:transparent;")
        lay.addWidget(icon)
        title = QLabel("Canlı Sohbet"); title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold)); title.setStyleSheet(f"color:{C.WHITE}; background:transparent;")
        lay.addWidget(title)
        meta = QLabel("Backend bağlantısı bekleniyor · görev verisi yok")
        self._chat_meta_lbl = meta
        meta.setFont(QFont("Segoe UI", 8)); meta.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); lay.addWidget(meta)
        lay.addStretch()
        self._voice_hud = VoiceHudWidget()
        lay.addWidget(self._voice_hud)
        self._center_state_lbl = self._pill("●  Dinleniyor…")
        lay.addWidget(self._center_state_lbl)
        return bar

    def _bubble(self, speaker: str, text: str, jarvis: bool = False):
        box = QFrame(); box.setObjectName("ChatBubble"); box.setStyleSheet(f"QFrame#ChatBubble {{ background:{'#0b2948' if not jarvis else '#0a3150'}; border:1px solid {C.BORDER}; border-radius:10px; }}")
        v = QVBoxLayout(box); v.setContentsMargins(14, 10, 14, 10); v.setSpacing(5)
        head = QLabel(f"{speaker}   {time.strftime('%H:%M')}"); head.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold)); head.setStyleSheet(f"color:{C.PRI if jarvis else C.TEXT_MED}; background:transparent;")
        body = QLabel(text); body.setTextFormat(Qt.TextFormat.PlainText); body.setWordWrap(True); body.setFont(QFont("Segoe UI", 10)); body.setStyleSheet(f"color:{C.WHITE}; background:transparent; line-height:140%;")
        v.addWidget(head); v.addWidget(body)
        if jarvis:
            foot = QLabel("Backend yanıtı")
            foot.setFont(QFont("Segoe UI", 8)); foot.setStyleSheet(f"color:{C.ACC}; background:transparent; border:none;"); v.addWidget(foot)
        return box

    def _build_chat_panel(self):
        panel = QWidget(); panel.setStyleSheet(f"background:{C.BG};")
        v = QVBoxLayout(panel); v.setContentsMargins(18, 18, 18, 12); v.setSpacing(12)
        self._chat_messages_widget = QWidget()
        self._chat_messages_widget.setObjectName("ChatMessages")
        self._chat_messages_widget.setStyleSheet(f"QWidget#ChatMessages {{ background:{C.BG}; }}")
        self._chat_messages_layout = QVBoxLayout(self._chat_messages_widget)
        self._chat_messages_layout.setSpacing(8)
        self._chat_messages_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_empty_lbl = QLabel("Henüz mesaj yok. Backend bağlantısı kurulunca konuşmalar burada görünür.")
        self._chat_empty_lbl.setWordWrap(True); self._chat_empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter); self._chat_empty_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none; padding:24px;")
        self._chat_messages_layout.addWidget(self._chat_empty_lbl)
        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chat_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._chat_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._chat_scroll.setWidget(self._chat_messages_widget)
        v.addWidget(self._chat_scroll, stretch=1)
        self._waveform = WaveformWidget(); v.addWidget(self._waveform)
        return panel

    def _build_task_flow(self):
        card = QFrame(); card.setObjectName("TaskFlow"); card.setFixedHeight(174); card.setStyleSheet(f"QFrame#TaskFlow {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:10px; }}")
        v = QVBoxLayout(card); v.setContentsMargins(16, 10, 16, 9); v.setSpacing(7); top = QHBoxLayout()
        title = QLabel("☷  Görev Yürütme Akışı"); title.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold)); title.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;"); top.addWidget(title); top.addStretch()
        self._task_status_lbl = QLabel("Görev yok — hazır"); self._task_status_lbl.setFont(QFont("Segoe UI", 8)); self._task_status_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); top.addWidget(self._task_status_lbl); v.addLayout(top)
        stages = QHBoxLayout(); stages.setSpacing(0); stages.setAlignment(Qt.AlignmentFlag.AlignVCenter); self._task_stage_nodes = {}
        for idx, (name, desc) in enumerate((("Planner", "Plan hazırla"), ("Research", "Veri tara"), ("Security", "Güvenliği kontrol et"), ("Auditor", "Son denetim"))):
            node = QVBoxLayout(); node.setSpacing(3); node.setContentsMargins(0, 0, 0, 0)
            circle = QLabel(str(idx + 1)); circle.setFixedSize(26, 26); circle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            circle.setStyleSheet(f"color:{C.TEXT_DIM}; background:{C.DARK}; border:1px solid {C.BORDER}; border-radius:13px;")
            label = QLabel(name); label.setAlignment(Qt.AlignmentFlag.AlignCenter); label.setFixedHeight(15); label.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
            detail = QLabel(desc); detail.setAlignment(Qt.AlignmentFlag.AlignCenter); detail.setFixedHeight(13); detail.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none; font-size:8px;")
            node.addWidget(circle, alignment=Qt.AlignmentFlag.AlignCenter); node.addWidget(label); node.addWidget(detail)
            cell = QWidget(); cell.setLayout(node); stages.addWidget(cell, stretch=1); self._task_stage_nodes[name] = circle
            if idx < 3:
                line = QFrame(); line.setFrameShape(QFrame.Shape.HLine); line.setFixedWidth(35); line.setStyleSheet(f"color:{C.BORDER}; background:{C.BORDER}; border:none;"); stages.addWidget(line, alignment=Qt.AlignmentFlag.AlignCenter)
        v.addLayout(stages)
        self._task_detail_lbl = QLabel("Yeni dosya veya komut gönderildiğinde görev akışı burada gösterilir."); self._task_detail_lbl.setFont(QFont("Segoe UI", 8)); self._task_detail_lbl.setMinimumHeight(18); self._task_detail_lbl.setMaximumHeight(22); self._task_detail_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); v.addWidget(self._task_detail_lbl)
        return card

    def _set_task_stages(self, active: str | None = None, completed: tuple[str, ...] = ()):
        for name, circle in self._task_stage_nodes.items():
            if name in completed:
                circle.setText("✓"); color = C.GREEN
            elif name == active:
                circle.setText("●"); color = C.PRI
            else:
                circle.setText(str(("Planner", "Research", "Security", "Auditor").index(name) + 1)); color = C.TEXT_DIM
            circle.setStyleSheet(f"color:{color}; background:{C.DARK}; border:1px solid {color}; border-radius:13px;")

    def _build_info_page(self, title: str, subtitle: str, rows: list[tuple[str, str]],
                         button_text: str | None = None, button_slot=None) -> QWidget:
        page = QFrame(); page.setObjectName("InfoPage")
        page.setStyleSheet(f"QFrame#InfoPage {{ background:{C.BG}; border:none; }}")
        layout = QVBoxLayout(page); layout.setContentsMargins(28, 28, 28, 20); layout.setSpacing(12)
        heading = QLabel(title); heading.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        heading.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;")
        layout.addWidget(heading)
        desc = QLabel(subtitle); desc.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        layout.addWidget(desc); layout.addSpacing(8)
        for label, value in rows:
            card = QFrame(); card.setStyleSheet(
                f"QFrame {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; }}"
            )
            row = QHBoxLayout(card); row.setContentsMargins(14, 12, 14, 12)
            key = QLabel(label); key.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            key.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;")
            val = QLabel(value); val.setWordWrap(True); val.setStyleSheet(
                f"color:{C.TEXT_MED}; background:transparent; border:none;"
            )
            row.addWidget(key); row.addStretch(); row.addWidget(val, stretch=1)
            layout.addWidget(card)
        if button_text and button_slot:
            button = QPushButton(button_text); button.setFixedHeight(38); button.clicked.connect(button_slot)
            button.setStyleSheet(
                f"QPushButton {{ color:{C.WHITE}; background:{C.PRI_DIM}; border:1px solid {C.PRI};"
                f" border-radius:6px; padding:6px 14px; }} QPushButton:hover {{ background:{C.PRI}; }}"
            )
            layout.addWidget(button)
        layout.addStretch()
        return page

    def _build_growth_page(self) -> QWidget:
        page = QFrame(); page.setObjectName("GrowthPage")
        page.setStyleSheet(f"QFrame#GrowthPage {{ background:{C.BG}; border:none; }}")
        layout = QVBoxLayout(page); layout.setContentsMargins(28, 28, 28, 20); layout.setSpacing(12)
        heading = QLabel("Kendi Gelişimi"); heading.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        heading.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;")
        layout.addWidget(heading)
        desc = QLabel("JARVIS sorunları analiz eder ve değişiklik önerir; canlı kodu izinsiz değiştirmez.")
        desc.setWordWrap(True); desc.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        layout.addWidget(desc)
        self._growth_status_lbl = QLabel("Hazır — henüz öneri oluşturulmadı")
        self._growth_status_lbl.setStyleSheet(f"color:{C.ACC}; background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; padding:10px;")
        layout.addWidget(self._growth_status_lbl)
        self._growth_report = QTextEdit(); self._growth_report.setReadOnly(True)
        self._growth_report.setPlaceholderText("Geliştirme önerileri burada görünecek.")
        self._growth_report.setStyleSheet(f"QTextEdit {{ color:{C.TEXT_MED}; background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; padding:10px; }}")
        layout.addWidget(self._growth_report, stretch=1)
        row = QHBoxLayout()
        propose = QPushButton("GÜVENLİ GELİŞİM RAPORU OLUŞTUR")
        propose.clicked.connect(self._create_improvement_proposal)
        propose.setStyleSheet(f"QPushButton {{ color:{C.WHITE}; background:{C.PRI_DIM}; border:1px solid {C.PRI}; border-radius:6px; padding:9px 14px; }} QPushButton:hover {{ background:{C.PRI}; }}")
        row.addWidget(propose); row.addStretch(); layout.addLayout(row)
        return page

    def _build_tasks_center_page(self) -> QWidget:
        page = QFrame(); page.setObjectName("TasksCenterPage")
        page.setStyleSheet(f"QFrame#TasksCenterPage {{ background:{C.BG}; border:none; }}")
        layout = QVBoxLayout(page); layout.setContentsMargins(28, 28, 28, 20); layout.setSpacing(12)
        heading = QLabel("Görev Merkezi"); heading.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        heading.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;"); layout.addWidget(heading)
        desc = QLabel("Agent Loop ve AI Beyin Takımı görevleri canlı veri dosyalarından izlenir.")
        desc.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); layout.addWidget(desc)
        self._tasks_summary = QTextEdit(); self._tasks_summary.setReadOnly(True)
        self._tasks_summary.setStyleSheet(f"QTextEdit {{ color:{C.TEXT_MED}; background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; padding:10px; }}")
        layout.addWidget(self._tasks_summary, stretch=1)
        return page

    def _build_team_center_page(self) -> QWidget:
        page = QFrame(); page.setObjectName("TeamCenterPage")
        page.setStyleSheet(f"QFrame#TeamCenterPage {{ background:{C.BG}; border:none; }}")
        layout = QVBoxLayout(page); layout.setContentsMargins(16, 12, 16, 10); layout.setSpacing(6)
        self._team_title_lbl = QLabel("JARVIS MİMARİSİ")
        self._team_title_lbl.setFont(QFont("Segoe UI", 18, QFont.Weight.DemiBold))
        self._team_title_lbl.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;")
        self._team_desc = QLabel(
            "Şematik bileşen görünümü — gerçek ağ cihazlarını göstermez"
        )
        self._team_desc.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        mode_row = QHBoxLayout()
        self._team_arch_btn = QPushButton("JARVIS MİMARİSİ")
        self._team_lan_btn = QPushButton("CANLI LAN")
        for button in (self._team_arch_btn, self._team_lan_btn):
            button.setCheckable(True)
            button.setStyleSheet(
                f"QPushButton {{ color:{C.TEXT_MED}; background:{C.PANEL}; border:1px solid {C.BORDER}; "
                "border-radius:7px; padding:7px 12px; }"
                f"QPushButton:checked {{ color:{C.WHITE}; background:{C.PRI_DIM}; border-color:{C.PRI}; }}"
            )
        mode_row.addWidget(self._team_arch_btn)
        mode_row.addWidget(self._team_lan_btn)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)
        layout.insertWidget(0, self._team_title_lbl)
        layout.insertWidget(1, self._team_desc)

        self._team_legend = QFrame()
        self._team_legend.setStyleSheet(
            f"QFrame {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:7px; }}"
        )
        legend_layout = QHBoxLayout(self._team_legend)
        legend_layout.setContentsMargins(9, 5, 9, 5)
        legend_layout.setSpacing(10)
        for color, text in (
            (C.GREEN, "Hazır · Boşta"), (C.PRI, "Bağlı · Çalışıyor"),
            ("#ffb44a", "Beklemede · Onay"), (C.RED, "Hata · Recovery gerekli"),
            (C.ACC2, "Heartbeat stale"), (C.TEXT_DIM, "Veri yok"),
        ):
            item = QWidget()
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(4)
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; background:transparent; border:none;")
            label = QLabel(text)
            label.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
            label.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
            item_layout.addWidget(dot)
            item_layout.addWidget(label)
            legend_layout.addWidget(item)
        legend_layout.addStretch(1)
        layout.addWidget(self._team_legend)

        self._team_graph_stack = QStackedWidget()
        self._network_canvas = SystemNetworkWidget()
        self._network_canvas.node_selected.connect(self._show_network_node_details)
        self._team_graph_stack.addWidget(self._network_canvas)

        self._lan_canvas = LanNetworkMapWidget()
        self._lan_canvas.node_selected.connect(self._show_lan_node_details)
        self._lan_local_networks = active_local_networks()
        self._lan_scope_edit = QLineEdit(suggested_scope(self._lan_local_networks))
        self._lan_scope_edit.setPlaceholderText("Örn. 192.168.1.0/24")
        self._lan_scope_edit.setMaximumWidth(210)
        self._lan_scope_edit.setToolTip("Yalnız bu bilgisayara bağlı, özel IPv4 /24 veya daha dar ağlar kabul edilir.")
        if len(self._lan_local_networks) == 1:
            self._lan_canvas.set_local_address(str(self._lan_local_networks[0].address))

        self._lan_controls = QFrame()
        self._lan_controls.setStyleSheet(
            f"QFrame {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; }}"
        )
        lan_layout = QVBoxLayout(self._lan_controls)
        lan_layout.setContentsMargins(9, 7, 9, 7)
        lan_layout.setSpacing(4)
        control_row = QHBoxLayout()
        scope_label = QLabel("Ağ aralığı")
        scope_label.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        self._lan_scan_btn = QPushButton("Yeniden tara")
        self._lan_live_btn = QPushButton("Canlı izlemeyi başlat")
        self._lan_live_btn.setCheckable(True)
        if not self._backend_enabled:
            self._lan_scan_btn.setEnabled(False)
            self._lan_live_btn.setEnabled(False)
        for button in (self._lan_scan_btn, self._lan_live_btn):
            button.setStyleSheet(
                f"QPushButton {{ color:{C.WHITE}; background:{C.PRI_DIM}; border:1px solid {C.PRI}; "
                "border-radius:6px; padding:6px 10px; }"
                f"QPushButton:hover, QPushButton:checked {{ background:{C.PRI}; }}"
            )
        control_row.addWidget(scope_label)
        control_row.addWidget(self._lan_scope_edit)
        control_row.addWidget(self._lan_scan_btn)
        control_row.addWidget(self._lan_live_btn)
        control_row.addStretch(1)
        lan_layout.addLayout(control_row)
        initial_lan_status = (
            "AI Ekip açıkken ağ 60 saniyede bir yenilenir."
            if self._backend_enabled
            else "UI-only mod · LAN taraması, backend ve ses kapalı."
        )
        self._lan_status_lbl = QLabel(initial_lan_status)
        self._lan_status_lbl.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
        lan_layout.addWidget(self._lan_status_lbl)
        self._lan_note_lbl = QLabel(
            "Yalnız ARP, ICMP veya denenmiş TCP portlarından yanıt alınan cihazlar gösterilir. "
            "Yanıt vermeyen cihazlar bulunamayabilir; bulunan sayı toplam cihaz sayısı değildir. "
            "Harita ağ topolojisini veya fiziksel bağlantıları göstermez."
        )
        self._lan_note_lbl.setWordWrap(True)
        self._lan_note_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; font-size:8pt; background:transparent; border:none;")
        lan_layout.addWidget(self._lan_note_lbl)
        self._team_graph_stack.addWidget(self._lan_canvas)
        layout.addWidget(self._lan_controls)
        self._lan_summary_panel = QFrame()
        self._lan_summary_panel.setObjectName("LanSummary")
        self._lan_summary_panel.setStyleSheet(
            f"QFrame#LanSummary {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:8px; }}"
        )
        summary_layout = QHBoxLayout(self._lan_summary_panel)
        summary_layout.setContentsMargins(9, 6, 9, 6)
        summary_layout.setSpacing(8)
        self._lan_summary_values = {}
        for key, caption, initial in (
            ("network", "AĞ ARALIĞI", "Veri yok"),
            ("last_scan", "SON TARAMA", "Henüz taranmadı"),
            ("count", "YANIT VEREN CİHAZ", "—"),
            ("duration", "SÜRE", "—"),
            ("result", "SONUÇ", "Veri yok"),
        ):
            tile = QFrame()
            tile.setStyleSheet("QFrame { background:transparent; border:none; }")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(4, 1, 4, 1)
            tile_layout.setSpacing(1)
            caption_label = QLabel(caption)
            caption_label.setFont(QFont("Segoe UI", 6, QFont.Weight.Bold))
            caption_label.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
            value_label = QLabel(initial)
            value_label.setWordWrap(True)
            value_label.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            value_label.setStyleSheet(f"color:{C.TEXT}; background:transparent; border:none;")
            tile_layout.addWidget(caption_label)
            tile_layout.addWidget(value_label)
            summary_layout.addWidget(tile, 1)
            self._lan_summary_values[key] = value_label
        layout.addWidget(self._lan_summary_panel)

        self._team_graph_panel = QFrame()
        self._team_graph_panel.setStyleSheet(
            f"QFrame {{ background:#050f1b; border:1px solid {C.BORDER}; border-radius:9px; }}"
        )
        graph_layout = QVBoxLayout(self._team_graph_panel)
        graph_layout.setContentsMargins(5, 5, 5, 5)
        graph_layout.addWidget(self._team_graph_stack, stretch=1)
        self._team_content_row = QHBoxLayout()
        self._team_content_row.setSpacing(8)
        self._team_content_row.addWidget(self._team_graph_panel, stretch=1)
        layout.addLayout(self._team_content_row, stretch=1)

        self._lan_snapshot: ScanSnapshot | None = None
        self._lan_scan_worker: LanScanWorker | None = None
        self._lan_restart_after_finish = False
        self._lan_refresh_tmr = QTimer(self)
        self._lan_refresh_tmr.setInterval(60_000)
        self._lan_refresh_tmr.timeout.connect(self._start_lan_scan)
        self._lan_scan_btn.clicked.connect(self._on_lan_scan_clicked)
        self._lan_live_btn.toggled.connect(self._set_lan_monitoring)
        self._team_arch_btn.clicked.connect(lambda: self._set_team_mode(0))
        self._team_lan_btn.clicked.connect(lambda: self._set_team_mode(1))

        details = QFrame()
        details.setObjectName("NetworkDetails")
        details.setMinimumWidth(min(330, max(270, int(self.width() * 0.22))))
        details.setMaximumWidth(350)
        details.setStyleSheet(
            f"QFrame#NetworkDetails {{ background:{C.PANEL}; border:1px solid {C.BORDER}; border-radius:9px; }}"
        )
        info = QVBoxLayout(details)
        info.setContentsMargins(13, 12, 13, 12)
        info.setSpacing(8)
        selected_heading = QLabel("SEÇİLEN BİLEŞEN")
        selected_heading.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        selected_heading.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        info.addWidget(selected_heading)
        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background:{C.BORDER}; border:none;")
        info.addWidget(separator)
        self._team_node_title = QLabel("JARVIS")
        self._team_node_title.setWordWrap(True)
        self._team_node_title.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        self._team_node_title.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;")
        info.addWidget(self._team_node_title)
        status_row = QHBoxLayout()
        self._team_node_dot = QLabel("●")
        self._team_node_dot.setFixedWidth(13)
        self._team_node_status = QLabel("Veri yok")
        self._team_node_status.setWordWrap(True)
        self._team_node_status.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        self._team_node_status.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        status_row.addWidget(self._team_node_dot)
        status_row.addWidget(self._team_node_status, stretch=1)
        info.addLayout(status_row)
        self._team_node_runtime = QLabel("")
        self._team_node_runtime.setWordWrap(True)
        self._team_node_runtime.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
        info.addWidget(self._team_node_runtime)
        self._team_meta_widgets = {}

        def add_meta_row(caption: str, attr: str):
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(7)
            name = QLabel(caption)
            name.setMinimumWidth(86)
            name.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
            value = QLabel("Veri yok")
            value.setWordWrap(True)
            value.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
            row.addWidget(name, alignment=Qt.AlignmentFlag.AlignTop)
            row.addWidget(value, stretch=1)
            info.addWidget(row_widget)
            self._team_meta_widgets[attr] = row_widget
            setattr(self, attr, value)
            return value

        self._team_node_heartbeat = add_meta_row("Son heartbeat", "_team_node_heartbeat")
        self._team_node_active_task = add_meta_row("Aktif görev", "_team_node_active_task")
        self._team_node_error = add_meta_row("Son hata", "_team_node_error")
        self._team_node_recovery = add_meta_row("Recovery", "_team_node_recovery")
        info.addSpacing(3)
        self._team_node_path = QLabel()
        self._team_node_path.setWordWrap(True)
        self._team_node_path.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;")
        info.addWidget(self._team_node_path)
        self._team_node_desc = QLabel()
        self._team_node_desc.setWordWrap(True)
        self._team_node_desc.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
        info.addWidget(self._team_node_desc)
        info.addStretch(1)
        self._team_details_panel = details
        self._team_content_row.addWidget(details)

        chat_strip = QFrame(); chat_strip.setObjectName("TeamChatStrip")
        chat_strip.setFixedHeight(34)
        chat_strip.setStyleSheet(
            f"QFrame#TeamChatStrip {{ background:#061427; border:1px solid {C.BORDER}; border-radius:8px; }}"
        )
        chat_row = QHBoxLayout(chat_strip); chat_row.setContentsMargins(11, 3, 11, 3); chat_row.setSpacing(8)
        chat_title = QLabel("KONUŞMA")
        chat_title.setStyleSheet(f"color:{C.PRI}; font-size:8pt; font-weight:600; background:transparent; border:none;")
        chat_row.addWidget(chat_title)
        self._team_chat_lbl = QLabel("Henüz mesaj yok")
        self._team_chat_lbl.setMinimumWidth(0)
        self._team_chat_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._team_chat_lbl.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
        chat_row.addWidget(self._team_chat_lbl, stretch=1)
        layout.addWidget(chat_strip)
        self._set_team_mode(0)
        self._update_lan_summary(None)
        return page

    def _show_network_node_details(self, node_id: str):
        if not hasattr(self, "_network_canvas"):
            return
        details = SystemNetworkWidget.DETAILS.get(node_id)
        if not details:
            return
        label = next((node[4] for node in SystemNetworkWidget.NODES if node[0] == node_id), node_id)
        state, runtime_detail = self._network_canvas.status_for_node(node_id)
        self._team_node_title.setText(label)
        self._team_node_status.setText(SystemNetworkWidget.STATUS_LABELS.get(state, "Veri yok"))
        color = self._network_canvas._status_color(node_id).name()
        self._team_node_dot.setStyleSheet(f"color:{color}; background:transparent; border:none;")
        self._team_node_runtime.setText(runtime_detail or "Bu bileşen için ek canlı ayrıntı yok.")
        self._team_node_path.setText(details[0])
        self._team_node_path.setToolTip(details[0])
        self._team_node_desc.setText(details[1])
        self._team_node_desc.setToolTip(details[1])
        brain_nodes = {"brain", "planner", "research", "coder", "security", "brain_memory", "executor", "auditor"}
        for row in self._team_meta_widgets.values():
            row.setVisible(node_id in brain_nodes)
        if node_id in brain_nodes:
            age = self._network_canvas.seconds_since_update(node_id)
            self._team_node_heartbeat.setText(
                f"UI son sinyali · {age} sn önce" if age is not None else "Henüz heartbeat alınmadı"
            )
            active_task, last_error, recovery = self._network_canvas.task_panel_details()
            self._team_node_active_task.setText(active_task)
            self._team_node_error.setText(last_error)
            self._team_node_recovery.setText(recovery)
        self._network_canvas.update()

    def _set_team_mode(self, index: int):
        self._team_graph_stack.setCurrentIndex(index)
        is_lan = index == 1
        self._current_team_mode = index
        self._lan_controls.setVisible(is_lan)
        self._lan_summary_panel.setVisible(is_lan)
        self._team_legend.setVisible(not is_lan)
        self._team_arch_btn.setChecked(not is_lan)
        self._team_lan_btn.setChecked(is_lan)
        if is_lan:
            self._team_title_lbl.setText("CANLI LAN")
            self._team_desc.setText("Gerçek yerel ağ cihazları — son tarama sonuçları")
            self._show_lan_node_details(self._lan_canvas.selected_node)
        else:
            self._team_title_lbl.setText("JARVIS MİMARİSİ")
            self._team_desc.setText("Şematik bileşen görünümü — gerçek ağ cihazlarını göstermez")
            self._show_network_node_details(self._network_canvas.selected_node)

    def _show_lan_node_details(self, node_id: str | None):
        snapshot = self._lan_snapshot
        for row in self._team_meta_widgets.values():
            row.hide()
        self._team_node_runtime.clear()
        self._team_node_dot.setStyleSheet(
            f"color:{C.GREEN if snapshot and snapshot.hosts else C.TEXT_DIM}; "
            "background:transparent; border:none;"
        )

        observation = next(
            (item for item in snapshot.hosts if str(item.address) == node_id), None
        ) if snapshot is not None and node_id is not None else None
        if observation is not None:
            self._team_node_title.setText("Yerel ağ cihazı")
            evidence_label = (
                "ARP yanıtı" if "arp" in observation.evidence else
                "ICMP yanıtı" if "icmp" in observation.evidence else "TCP yanıtı"
            )
            self._team_node_status.setText(evidence_label)
            self._team_node_dot.setStyleSheet(f"color:{C.GREEN}; background:transparent; border:none;")
            self._team_node_path.setText(f"IPv4 · {observation.address}")
            ports = ", ".join(service_labels(observation.open_ports)) or "Test edilen TCP portlarında açık servis yok"
            self._team_node_desc.setText(f"{ports} · servis adı yalnızca port numarasından çıkarılır.")
        else:
            self._team_node_title.setText("Keşfedilmiş cihaz yok")
            if snapshot is None:
                address = (
                    str(self._lan_local_networks[0].address)
                    if len(self._lan_local_networks) == 1
                    else "etkin yerel arayüz seçilmedi"
                )
                self._team_node_status.setText("Tarama başlatılmadı")
                self._team_node_path.setText(
                    f"Yerel arayüz · {address} (tarama bulgusu değil)"
                )
                self._team_node_desc.setText(
                    "Henüz tarama sonucu yok. Yerel arayüz bilgisi tarama bulgusu değildir; "
                    "haritada yalnızca yanıt veren cihazlar gösterilir."
                )
            else:
                self._team_node_status.setText(
                    "Tarama durduruldu" if snapshot.cancelled else "Yanıt veren cihaz yok"
                )
                route = (
                    f"Ağ geçidi rota kaydı · {snapshot.gateway} (yanıtı doğrulanmadı)"
                    if snapshot.gateway is not None else ""
                )
                local = (
                    f"Yerel arayüz · {snapshot.local_address} (tarama bulgusu değil)"
                )
                self._team_node_path.setText(" · ".join(part for part in (route, local) if part))
                route_note = (
                    "Ağ geçidinin yanıtı doğrulanmadı; "
                    if snapshot.gateway is not None else ""
                )
                self._team_node_desc.setText(
                    f"{route_note}Rota ve yerel arayüz bilgileri tarama bulgusu değildir. "
                    "Haritada yalnızca yanıt veren cihazlar gösterilir."
                )
        for label in (self._team_node_path, self._team_node_desc):
            label.setToolTip(label.text())

    def _update_lan_summary(self, snapshot: ScanSnapshot | None):
        if snapshot is None:
            values = {
                "network": "Veri yok", "last_scan": "Henüz taranmadı",
                "count": "—", "duration": "—", "result": "Veri yok",
            }
        else:
            try:
                scanned = datetime.fromisoformat(snapshot.scanned_at.replace("Z", "+00:00"))
                if scanned.tzinfo is not None:
                    scanned = scanned.astimezone()
                scan_time = scanned.strftime("%d.%m.%Y %H:%M:%S")
            except (TypeError, ValueError):
                scan_time = str(snapshot.scanned_at)
            values = {
                "network": str(snapshot.network),
                "last_scan": scan_time,
                "count": f"{len(snapshot.hosts)} yanıt veren cihaz",
                "duration": f"{snapshot.elapsed_seconds:.1f} sn",
                "result": "Durduruldu · kısmi" if snapshot.cancelled else "Kısmi keşif",
            }
        for key, value in values.items():
            label = self._lan_summary_values[key]
            label.setText(value)
            label.setToolTip(value)

    def _set_lan_monitoring(self, enabled: bool):
        self._lan_refresh_tmr.stop()
        if not self._backend_enabled:
            previous = self._lan_live_btn.blockSignals(True)
            self._lan_live_btn.setChecked(False)
            self._lan_live_btn.blockSignals(previous)
            self._lan_status_lbl.setText("UI-only mod · LAN taraması, backend ve ses kapalı.")
            return
        if not enabled:
            self._lan_live_btn.setText("Canlı izlemeyi başlat")
            worker = self._lan_scan_worker
            if worker is not None and worker.isRunning():
                self._lan_restart_after_finish = False
                worker.requestInterruption()
                self._lan_status_lbl.setText("Canlı izleme kapalı · etkin tarama durduruluyor…")
            else:
                self._lan_status_lbl.setText("Canlı izleme kapalı · son gözlemler ekranda.")
            return
        try:
            network = parse_scope(self._lan_scope_edit.text())
            interfaces = active_local_networks()
            if not any(item.network == network for item in interfaces):
                raise NetworkScopeError("CIDR etkin yerel ağ arayüzüyle eşleşmiyor.")
        except NetworkScopeError as exc:
            previous = self._lan_live_btn.blockSignals(True)
            self._lan_live_btn.setChecked(False)
            self._lan_live_btn.blockSignals(previous)
            self._lan_live_btn.setText("Canlı izlemeyi başlat")
            self._lan_status_lbl.setText(f"Canlı izleme başlamadı · {exc}")
            return
        self._lan_live_btn.setText("Canlı izleme açık · 60 sn")
        self._lan_refresh_tmr.start()
        self._start_lan_scan()
    def _on_lan_scan_clicked(self):
        worker = self._lan_scan_worker
        if worker is not None and worker.isRunning():
            self._lan_restart_after_finish = False
            worker.requestInterruption()
            self._lan_scan_btn.setText("Durduruluyor…")
            self._lan_status_lbl.setText("Etkin bağlantı denemeleri bitince tarama duracak.")
            return
        self._start_lan_scan()

    def _start_lan_scan(self):
        if not self._backend_enabled:
            self._lan_status_lbl.setText("UI-only mod · LAN taraması, backend ve ses kapalı.")
            return
        worker = self._lan_scan_worker
        if worker is not None and worker.isRunning():
            if self._lan_live_btn.isChecked():
                self._lan_restart_after_finish = True
            return
        try:
            network = parse_scope(self._lan_scope_edit.text())
            interfaces = active_local_networks()
            if not any(item.network == network for item in interfaces):
                raise NetworkScopeError("CIDR etkin, bu bilgisayara bağlı özel IPv4 /24 ağıyla eşleşmiyor.")
        except NetworkScopeError as exc:
            self._lan_status_lbl.setText(f"Tarama başlatılmadı · {exc}")
            return

        worker = LanScanWorker(str(network), interfaces, self)
        self._lan_scan_worker = worker
        self._lan_scan_btn.setText("Taramayı durdur")
        self._lan_status_lbl.setText(f"{network} · canlı cihaz ve TCP port yanıtları aranıyor…")
        worker.progress.connect(self._on_lan_scan_progress)
        worker.result_ready.connect(self._on_lan_scan_result)
        worker.failed.connect(self._on_lan_scan_failed)
        worker.finished.connect(self._on_lan_scan_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
    def _on_lan_scan_progress(self, completed: int, total: int):
        self._lan_status_lbl.setText(f"Yerel ağ taranıyor · {completed}/{total} adres tamamlandı")

    def _on_lan_scan_result(self, snapshot: ScanSnapshot):
        self._lan_snapshot = snapshot
        self._lan_canvas.set_snapshot(snapshot)
        self._update_lan_summary(snapshot)
        self._show_lan_node_details(self._lan_canvas.selected_node)
        open_ports = sum(len(host.open_ports) for host in snapshot.hosts)
        stamp = snapshot.scanned_at.replace("T", " ")
        if snapshot.cancelled:
            state = "Durduruldu"
        else:
            state = "Tarama tamamlandı"
        self._lan_status_lbl.setText(
            f"{state} · {len(snapshot.hosts)} yanıt veren cihaz · {open_ports} açık TCP portu · "
            f"keşif kapsamlı değildir · "
            f"{snapshot.network} · {stamp}"
        )

    def _on_lan_scan_failed(self, message: str):
        self._lan_status_lbl.setText(f"Tarama başarısız · {message[:240]}")

    def _on_lan_scan_finished(self):
        self._lan_scan_worker = None
        self._lan_scan_btn.setText("Şimdi tara")
        if (
            self._lan_restart_after_finish
            and self._lan_live_btn.isChecked()
            and self._chat_stack.currentWidget() is self._team_page
        ):
            self._lan_restart_after_finish = False
            QTimer.singleShot(0, self._start_lan_scan)
        else:
            self._lan_restart_after_finish = False
    def _stop_lan_scan(self, wait: bool = False):
        self._lan_refresh_tmr.stop()
        worker = self._lan_scan_worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            if wait:
                worker.wait()

    def _apply_network_status(self, component: str, status: str, detail: str):
        if not hasattr(self, "_network_canvas"):
            return
        if component == "tool":
            self._network_canvas.update_tool_status(detail, status)
            normalized = str(status or "unknown").strip().lower()
            status_label = SystemNetworkWidget.STATUS_LABELS.get(normalized, normalized)
            tool_text = str(detail or "Bilinmeyen araç")
            prefix = "Aktif araç" if normalized in {"running", "pending"} else "Son araç"
            self._last_tool_context = f"{prefix} · {tool_text} · {status_label}"
            self._set_composer_context(self._last_tool_context)
        else:
            self._network_canvas.update_runtime_status(component, status, detail)
        self._show_network_node_details(self._network_canvas.selected_node)

    def _set_composer_context(self, text: str):
        if not hasattr(self, "_composer_context_lbl"):
            return
        value = str(text or "").strip()
        self._composer_context_lbl.setText(value)
        self._composer_context_lbl.setVisible(bool(value))

    def _read_task_files(self) -> list[dict]:
        by_id: dict[str, dict] = {}
        anonymous: list[dict] = []
        for path in (memory_dir() / "agent_tasks.json", tasks_dir() / "brain_tasks.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
                if isinstance(data, list):
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        item = {**item, "_source": path.name}
                        task_id = str(item.get("id", "")).strip()
                        if task_id:
                            previous = by_id.get(task_id)
                            if previous is None or str(item.get("updated_at", item.get("created_at", ""))) >= str(previous.get("updated_at", previous.get("created_at", ""))):
                                by_id[task_id] = item
                        else:
                            anonymous.append(item)
            except Exception as exc:
                # Do not emit this every 1.5 s: a transient half-written JSON
                # file would otherwise flood the activity feed and make the
                # UI look as if old events were replaying.
                if not getattr(self, "_task_read_error", False):
                    self._task_read_error = True
                    self._log_sig.emit(f"SYS: Görev kaydı okunamadı: {exc}")
        self._task_read_error = False

        board_db = memory_dir() / "agent_board.db"
        if board_db.is_file():
            try:
                with sqlite3.connect(board_db.as_uri() + "?mode=ro", uri=True, timeout=0.1) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, description, status, started_at, finished_at "
                        "FROM jobs ORDER BY started_at DESC LIMIT 100"
                    ).fetchall()
                for row in rows:
                    item = {
                        "id": row["id"], "goal": row["description"], "status": row["status"],
                        "updated_at": row["finished_at"] or row["started_at"],
                        "_source": "agent_board.db",
                    }
                    task_id = str(item.get("id", "")).strip()
                    if task_id:
                        previous = by_id.get(task_id)
                        if previous is None or str(item.get("updated_at", "")) >= str(
                            previous.get("updated_at", previous.get("created_at", ""))
                        ):
                            by_id[task_id] = item
            except (OSError, sqlite3.Error, ValueError):
                # The database may be absent, busy, or from an older schema.
                # The UI observer must not create or mutate it.
                pass

        tasks = list(by_id.values()) + anonymous
        return sorted(
            tasks,
            key=lambda item: str(item.get("updated_at", item.get("created_at", ""))),
            reverse=True,
        )

    def _refresh_task_center(self):
        if not hasattr(self, "_task_status_lbl"):
            return
        tasks = self._read_task_files()
        counts = {status: sum(1 for task in tasks if task.get("status") == status) for status in
                  ("pending", "running", "awaiting_approval", "completed", "done", "failed", "cancelled")}
        recovery = next(
            (task for task in tasks if SystemNetworkWidget._task_state(task) == "recovery_required"),
            None,
        )
        active = recovery or next(
            (task for task in tasks if task.get("status") in
             ("pending", "running", "awaiting_approval", "waiting_approval")),
            None,
        )
        if active:
            status = SystemNetworkWidget._task_state(active)
            status_text = {
                "pending": "Beklemede", "running": "Çalışıyor",
                "awaiting_approval": "Onay bekliyor", "recovery_required": "Recovery gerekli",
            }.get(status, status)
            goal = str(active.get("goal", active.get("name", "Görev")))
            self._task_status_lbl.setText(f"{status_text} · {active.get('id', '—')}")
            self._task_detail_lbl.setText(goal[:150])
            if status == "pending":
                self._set_task_stages("Planner", ())
            elif status == "running":
                self._set_task_stages("Research", ("Planner",))
            elif status in {"awaiting_approval", "waiting_approval"}:
                self._set_task_stages("Security", ("Planner", "Research"))
            else:
                self._set_task_stages(None, ())
        elif tasks:
            latest = tasks[0]
            latest_status = str(latest.get("status", "bilinmiyor"))
            status_text = {
                "completed": "Tamamlandı",
                "done": "Tamamlandı",
                "failed": "Başarısız",
                "cancelled": "İptal edildi",
            }.get(latest_status, latest_status)
            self._task_status_lbl.setText(f"Son görev · {status_text}")
            self._task_detail_lbl.setText(str(latest.get("goal", latest.get("name", "Görev")))[:150])
            if latest_status in ("completed", "done"):
                self._set_task_stages(None, ("Planner", "Research", "Security", "Auditor"))
            elif latest_status == "failed":
                self._set_task_stages("Auditor", ("Planner", "Research", "Security"))
            else:
                self._set_task_stages(None, ())
        else:
            self._task_status_lbl.setText("Görev yok — hazır")
            self._task_detail_lbl.setText("Yeni görev verdiğinizde durum burada canlı gösterilir.")
            self._set_task_stages(None, ())
        if hasattr(self, "_tasks_summary"):
            self._tasks_summary.setPlainText(self._format_task_summary(tasks, counts))
        if hasattr(self, "_network_canvas"):
            self._network_canvas.update_task_records(tasks)
            self._show_network_node_details(self._network_canvas.selected_node)
        awaiting_approval = next(
            (task for task in tasks if task.get("status") in {"awaiting_approval", "waiting_approval"}),
            None,
        )
        if awaiting_approval:
            task_name = str(awaiting_approval.get("goal", awaiting_approval.get("name", "Görev")))[:90]
            self._set_composer_context(f"Onay bekliyor · {task_name}")
        elif self._last_tool_context:
            self._set_composer_context(self._last_tool_context)

    @staticmethod
    def _format_task_summary(tasks: list[dict], counts: dict[str, int]) -> str:
        lines = [
            f"Toplam görev: {len(tasks)}",
            f"Bekleyen: {counts['pending']}   Çalışan: {counts['running']}   Onay: {counts['awaiting_approval']}",
            f"Tamamlanan: {counts['completed'] + counts['done']}   Başarısız: {counts['failed']}   İptal: {counts['cancelled']}",
            "", "SON GÖREVLER",
        ]
        for task in tasks[:12]:
            goal = str(task.get("goal", task.get("name", "Görev"))).replace("\n", " ")
            lines.append(f"[{task.get('status', '?')}] {task.get('id', '—')} · {goal[:110]}")
        return "\n".join(lines)

    def _create_improvement_proposal(self):
        tasks = self._read_task_files()
        failed = [task for task in tasks if task.get("status") == "failed"]
        proposal_dir = memory_dir() / "self_improvement" / "proposals"
        proposal_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        report = ["# JARVIS Güvenli Gelişim Önerisi", "", "Bu rapor yalnızca analiz ve öneridir.", "Kod değişikliği uygulanmadı.", ""]
        report.append(f"## Gözlenen görevler\n- Toplam: {len(tasks)}\n- Başarısız: {len(failed)}")
        if failed:
            report.append("\n## İncelenecek başarısız görevler")
            report.extend(f"- `{task.get('id', '—')}`: {task.get('goal', task.get('name', ''))}" for task in failed[:10])
        report.extend(["", "## Güvenlik kapısı", "Öneri önce izole testte doğrulanmalı, sonra kullanıcı onayı olmadan canlı koda uygulanmamalıdır."])
        target = proposal_dir / f"proposal_{stamp}.md"; target.write_text("\n".join(report) + "\n", encoding="utf-8")
        self._growth_report.setPlainText("\n".join(report))
        self._growth_status_lbl.setText(f"Öneri hazır · {target.name} · uygulanmadı")
        self._log_sig.emit(f"SELF_IMPROVEMENT: Güvenli öneri oluşturuldu: {target.name}")

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - self._right_panel_width - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._cam_preview.hide()
            self._hud_cam_stack.setCurrentIndex(self._camera_page_index)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.setText("Canlı kamera akışı bekleniyor")
            self._cam_live_lbl.setPixmap(QPixmap())
            self._cam_preview.hide()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        if self._cam_thread and self._cam_thread.is_alive():
            return
        self._cam_stop.clear(); self._cam_stream_sig.emit(True)
        self._cam_thread = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        self._cam_thread.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads(api_keys_path().read_text(encoding="utf-8"))
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 255, 136)
        DIM    = (0, 120, 70)
        DARK   = (0, 6, 10)
        GLOW   = (0, 200, 100)
        WHITE  = (220, 255, 232)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = Path.home() / "Desktop"

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Masaüstü kısayolu oluşturuldu.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 430
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - self._right_panel_width - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    def _update_metrics(self):
        global _metrics
        if _metrics is None:
            _metrics = _SysMetrics()
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")
        if hasattr(self, "_ring_cpu"): self._ring_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")
        if hasattr(self, "_ring_mem"): self._ring_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
            if hasattr(self, "_ring_gpu"): self._ring_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")
            if hasattr(self, "_ring_gpu"): self._ring_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")
        # Connection/device status is intentionally derived from live local
        # signals instead of remaining at the design-time "unknown" value.
        try:
            active_ifaces = [name for name, stat in psutil.net_if_stats().items()
                             if stat.isup and not name.lower().startswith(("loopback", "lo"))]
            self._network_lbl.setText("Bağlı" if active_ifaces else "Bağlantı yok")
            self._network_lbl.setStyleSheet(
                f"color:{C.GREEN if active_ifaces else C.RED}; background:transparent; border:none;"
            )
        except Exception:
            self._network_lbl.setText("Bilinmiyor")
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            usable = sum(1 for d in devices if d.get("max_input_channels", 0) or d.get("max_output_channels", 0))
            self._device_count_lbl.setText(f"{usable} cihaz")
        except Exception:
            self._device_count_lbl.setText("Ses API bekleniyor")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("ÇALIŞMA  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("İŞL  --")






    def _build_header(self):
        frame = QFrame()
        frame.setObjectName("Header")
        frame.setFixedHeight(86)
        frame.setStyleSheet(
            f"QFrame#Header {{ background:#061427; border-bottom:1px solid {C.BORDER}; }}"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(18, 7, 18, 7)
        layout.setSpacing(14)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel("◉  JARVIS")
        title.setFont(QFont("Segoe UI", 23, QFont.Weight.Bold))
        title.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;")
        subtitle = QLabel("PREMIUM AI WORKSTATION")
        subtitle.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        subtitle.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        layout.addLayout(brand)

        status_row = QWidget()
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(7)
        self._status_cards = {}
        for key, icon, name in (
            ("system", "◈", "Sistem"),
            ("gemini", "↔", "Gemini Live"),
            ("microphone", "♩", "Mikrofon"),
            ("speaker", "◖", "Hoparlör"),
        ):
            card = QFrame()
            card.setObjectName("TopStatusCard")
            card.setMinimumWidth(112)
            card.setStyleSheet(
                f"QFrame#TopStatusCard {{ background:#071a30; border:1px solid {C.BORDER}; border-radius:8px; }}"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(9, 5, 9, 5)
            card_layout.setSpacing(1)
            title_row = QHBoxLayout()
            title_row.setSpacing(5)
            dot = QLabel("●")
            dot.setFixedWidth(11)
            icon_label = QLabel(icon)
            icon_label.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;")
            title_label = QLabel(name)
            title_label.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            title_label.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
            title_row.addWidget(dot)
            title_row.addWidget(icon_label)
            title_row.addWidget(title_label)
            title_row.addStretch(1)
            card_layout.addLayout(title_row)
            state_label = QLabel("Veri yok")
            state_label.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
            state_label.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
            updated_label = QLabel("Güncelleme bekleniyor")
            updated_label.setFont(QFont("Courier New", 6))
            updated_label.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
            card_layout.addWidget(state_label)
            card_layout.addWidget(updated_label)
            status_layout.addWidget(card, 1)
            self._status_cards[key] = {
                "frame": card, "dot": dot, "state": state_label,
                "updated": updated_label, "icon": icon_label,
            }
        layout.addWidget(status_row, stretch=1)

        clock_panel = QVBoxLayout()
        clock_panel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        clock_panel.setSpacing(2)
        self._clock_lbl = QLabel("--:--:--")
        self._clock_lbl.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._clock_lbl.setStyleSheet(f"color:{C.ACC2}; background:transparent; border:none;")
        self._date_lbl = QLabel("--/--/----")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._date_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        clock_panel.addWidget(self._clock_lbl)
        clock_panel.addWidget(self._date_lbl)
        layout.addLayout(clock_panel)

        self._header_state_lbl = self._status_cards["gemini"]["state"]
        self._online_lbl = self._header_state_lbl
        self._connection_detail_lbl = self._status_cards["gemini"]["updated"]
        self._refresh_header_statuses()
        return frame

    def _tick_clock(self):
        now = datetime.now().astimezone()
        self._clock_lbl.setText(now.strftime("%H:%M:%S"))
        self._date_lbl.setText(now.strftime("%d %b %Y"))
        self._refresh_header_statuses()

    def _touch_status(self, key: str):
        self._status_updated_at[key] = datetime.now().astimezone()

    def _refresh_header_statuses(self):
        if not hasattr(self, "_status_cards"):
            return
        connected_states = {"LISTENING", "SPEAKING", "THINKING"}
        gemini_map = {
            "CONNECTING": ("Bağlanıyor", "#ffb44a", "Gemini Live bağlantısı kuruluyor"),
            "AUTH_REQUIRED": ("Kimlik doğrulaması gerekli", C.RED, "API anahtarı doğrulaması gerekli"),
            "ERROR": ("Hata", C.RED, "Canlı oturum hata durumunda"),
            "LOCAL_ONLY": ("Kapalı · UI-only", C.TEXT_DIM, "Backend bağlantısı başlatılmadı"),
            "LISTENING": ("Bağlı · Dinliyor", C.PRI, "Canlı oturum açık"),
            "SPEAKING": ("Bağlı · Yanıt veriyor", C.PRI, "Yanıt seslendiriliyor"),
            "THINKING": ("Bağlı · İşliyor", C.PRI, "Araç çağrısı işleniyor"),
            "SLEEPING": ("Bağlı değil", C.TEXT_DIM, "Canlı oturum kapandı"),
            "UNKNOWN": ("Bilinmiyor", C.TEXT_DIM, "Henüz bağlantı durumu alınmadı"),
        }
        gemini_state, gemini_color, gemini_tip = gemini_map.get(
            self._connection_state, ("Bilinmiyor", C.TEXT_DIM, "Henüz bağlantı durumu alınmadı")
        )
        mic_name = str(self._mic_device_name or "").strip()
        mic_missing = not mic_name or mic_name.upper() in {"UNKNOWN", "BILINMIYOR", "BİLİNMİYOR", "N/A"}
        mic_unavailable = mic_name.upper().startswith(("YOK", "DEVRE KESİCİ", "HATA"))
        if not self._backend_enabled:
            mic_state, mic_color, mic_tip = "Kapalı · UI-only", C.TEXT_DIM, "UI-only modunda ses başlatılmadı"
        elif mic_missing:
            mic_state, mic_color, mic_tip = "Bilinmiyor", C.TEXT_DIM, "Mikrofon cihazı henüz bildirilmedi"
        elif mic_unavailable:
            mic_state, mic_color, mic_tip = "Kullanılamıyor", C.RED, mic_name
        elif self._muted:
            mic_state, mic_color, mic_tip = "Sessiz", "#ffb44a", mic_name
        else:
            mic_state, mic_color, mic_tip = "Hazır", C.GREEN, mic_name

        speaker_name = str(self._speaker_device_name or "").strip()
        speaker_missing = not speaker_name or speaker_name.upper() in {"UNKNOWN", "BILINMIYOR", "BİLİNMİYOR", "N/A"}
        speaker_unavailable = speaker_name.upper().startswith(("YOK", "HATA"))
        if not self._backend_enabled:
            speaker_state, speaker_color, speaker_tip = "Kapalı · UI-only", C.TEXT_DIM, "UI-only modunda ses başlatılmadı"
        elif speaker_missing:
            speaker_state, speaker_color, speaker_tip = "Bilinmiyor", C.TEXT_DIM, "Hoparlör cihazı henüz bildirilmedi"
        elif speaker_unavailable:
            speaker_state, speaker_color, speaker_tip = "Kullanılamıyor", C.RED, speaker_name
        else:
            speaker_state, speaker_color, speaker_tip = "Hazır", C.GREEN, speaker_name

        if not self._backend_enabled:
            system_state, system_color, system_tip = "Yerel arayüz", C.TEXT_DIM, "Backend ve ses başlatılmadı"
        elif self._connection_state in {"AUTH_REQUIRED", "ERROR"}:
            system_state, system_color, system_tip = "Eylem gerekli", C.RED, "Bağlantı veya kimlik doğrulama sorunu"
        elif self._connection_state == "CONNECTING":
            system_state, system_color, system_tip = "Bağlanıyor", "#ffb44a", "Canlı oturum henüz doğrulanmadı"
        elif self._connection_state in connected_states and mic_state in {"Hazır", "Sessiz"} and speaker_state == "Hazır":
            system_state, system_color, system_tip = "Hazır", C.GREEN, "Canlı oturum ve ses cihazları bildirildi"
        elif self._connection_state in connected_states:
            system_state, system_color, system_tip = "Cihaz verisi eksik", "#ffb44a", "Mikrofon veya hoparlör durumu doğrulanmadı"
        elif self._connection_state == "SLEEPING":
            system_state, system_color, system_tip = "Bağlı değil", C.TEXT_DIM, "Canlı oturum beklemede"
        else:
            system_state, system_color, system_tip = "Bilinmiyor", C.TEXT_DIM, "Henüz yeterli durum verisi yok"

        values = {
            "system": (system_state, system_color, system_tip),
            "gemini": (gemini_state, gemini_color, gemini_tip),
            "microphone": (mic_state, mic_color, mic_tip),
            "speaker": (speaker_state, speaker_color, speaker_tip),
        }
        for key, (label, color, tip) in values.items():
            card = self._status_cards[key]
            card["state"].setText(label)
            card["state"].setStyleSheet(f"color:{color}; background:transparent; border:none;")
            card["dot"].setStyleSheet(f"color:{color}; background:transparent; border:none;")
            card["frame"].setToolTip(tip)
            updated = self._status_updated_at.get(key)
            if key == "system":
                observed = [self._status_updated_at[name] for name in ("gemini", "microphone", "speaker") if self._status_updated_at.get(name)]
                updated = max(observed) if observed else None
            card["updated"].setText(f"Güncellendi: {updated.strftime('%H:%M:%S')}" if updated else "Güncelleme bekleniyor")





    def _build_left_panel(self):
        panel = QFrame()
        panel.setObjectName("LeftPanel")
        panel.setFixedWidth(self._left_panel_width)
        panel.setStyleSheet(
            f"QFrame#LeftPanel {{ background:#061427; border-right:1px solid {C.BORDER}; }}"
        )
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(7)
        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("RAM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TEMP", "#ff6688")
        for bar in (self._bar_cpu, self._bar_mem, self._bar_net, self._bar_gpu, self._bar_tmp):
            bar.hide()

        self._nav_buttons = {}
        nav_groups = (
            ("ÇALIŞMA", (("♬", "Canlı Sohbet"), ("☷", "Görevler"), ("♧", "AI Ekip"))),
            ("SİSTEM", (("✦", "Gelişim"), ("▱", "Dosyalar"), ("▣", "Sistem"), ("⚙", "Ayarlar"))),
        )
        for group_index, (group_title, items) in enumerate(nav_groups):
            heading = QLabel(group_title)
            heading.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            heading.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
            layout.addSpacing(7 if group_index else 0)
            layout.addWidget(heading)
            separator = QFrame()
            separator.setFixedHeight(1)
            separator.setStyleSheet(f"background:{C.BORDER}; border:none;")
            layout.addWidget(separator)
            for icon, name in items:
                button = QPushButton(f"  {icon}    {name}")
                button.setObjectName("NavButton")
                button.setFixedHeight(43)
                button.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                active = name == "Canlı Sohbet"
                button.setStyleSheet(
                    f"QPushButton#NavButton {{ color:{C.WHITE if active else C.TEXT_MED}; "
                    f"background:{'#0b2948' if active else 'transparent'}; border:none; "
                    "border-radius:9px; padding-left:5px; text-align:left; }"
                    f"QPushButton#NavButton:hover, QPushButton#NavButton:focus {{ color:{C.WHITE}; background:#0a223d; }}"
                )
                button.clicked.connect(lambda _checked=False, target=name: self._navigate(target))
                self._nav_buttons[name] = button
                layout.addWidget(button)

        layout.addSpacing(8)
        device_title = QLabel("SES AYGITLARI")
        device_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        device_title.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        layout.addWidget(device_title)
        self._mic_lbl = QLabel("🎤  Mikrofon: bilinmiyor")
        self._speaker_lbl = QLabel("🔊  Hoparlör: bilinmiyor")
        for label in (self._mic_lbl, self._speaker_lbl):
            label.setWordWrap(True)
            label.setFont(QFont("Segoe UI", 8))
            label.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;")
            layout.addWidget(label)

        self._mute_btn = QPushButton()
        self._mute_btn.setObjectName("MuteButton")
        self._mute_btn.setFixedHeight(34)
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        layout.addWidget(self._mute_btn)
        self._style_mute_btn()
        if not self._backend_enabled:
            self._mute_btn.setText("MİKROFON · UI-ONLY")
            self._mute_btn.setEnabled(False)
            self._mute_btn.setToolTip("Ses için JARVIS'i --ui-only olmadan başlatın.")

        layout.addStretch(1)
        self._health_lbl = QLabel("●   UI yerel · bağlantı ayrı   ›")
        self._health_lbl.setFixedHeight(38)
        self._health_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self._health_lbl.setStyleSheet(
            f"color:{C.TEXT_MED}; background:#071c32; border:1px solid {C.BORDER}; "
            "border-radius:9px; padding-left:10px;"
        )
        layout.addWidget(self._health_lbl)
        self._uptime_lbl = QLabel()
        self._proc_lbl = QLabel()
        return panel

    def _build_right_panel(self):
        w = QFrame(); w.setObjectName("RightPanel"); w.setFixedWidth(self._right_panel_width); w.setStyleSheet(f"QFrame#RightPanel {{ background:#061427; border-left:1px solid {C.BORDER}; }}")
        lay = QVBoxLayout(w); lay.setContentsMargins(18, 16, 18, 16); lay.setSpacing(10)
        def section(title, icon="◈"):
            row = QHBoxLayout(); ico = QLabel(icon); ico.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold)); ico.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;"); lab = QLabel(title); lab.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold)); lab.setStyleSheet(f"color:{C.WHITE}; background:transparent; border:none;"); row.addWidget(ico); row.addWidget(lab); row.addStretch(); lay.addLayout(row)
        section("Sistem Telemetrisi", "⌁"); telemetry = QFrame(); telemetry.setObjectName("TelemetryPanel"); telemetry.setStyleSheet(f"QFrame#TelemetryPanel {{ background:#0a2038; border:1px solid {C.BORDER}; border-radius:14px; }}")
        tv = QVBoxLayout(telemetry); tv.setContentsMargins(14, 10, 14, 8); tv.setSpacing(6)
        for bar in (self._bar_cpu, self._bar_mem, self._bar_gpu): bar.show(); tv.addWidget(bar)
        rings = QHBoxLayout(); self._ring_cpu = CircularMetric("CPU", C.PRI, telemetry); self._ring_mem = CircularMetric("RAM", C.ACC2, telemetry); self._ring_gpu = CircularMetric("GPU", C.ACC, telemetry)
        for ring in (self._ring_cpu, self._ring_mem, self._ring_gpu): rings.addWidget(ring)
        tv.addLayout(rings); lay.addWidget(telemetry)
        section("Bağlantı ve Cihaz Durumu", "◎"); conn = QFrame(); conn.setObjectName("ConnectionPanel"); conn.setStyleSheet(f"QFrame#ConnectionPanel {{ background:#0a2038; border:1px solid {C.BORDER}; border-radius:14px; }}")
        cv = QVBoxLayout(conn); cv.setContentsMargins(14, 7, 14, 7); self._connection_rows = {}
        for icon, name, key in (("◉", "Ana ağ", "network"), ("◌", "İnternet / Gemini", "internet"),
                                ("⌘", "Ses cihazları", "devices"), ("🎙", "Mikrofon", "microphone"),
                                ("🔊", "Hoparlör", "speaker")):
            r = QHBoxLayout(); i = QLabel(icon); i.setStyleSheet(f"color:{C.PRI}; background:transparent; border:none;"); n = QLabel(name); n.setStyleSheet(f"color:{C.TEXT_MED}; background:transparent; border:none;"); d = QLabel("Hazırlanıyor"); d.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); d.setAlignment(Qt.AlignmentFlag.AlignRight); self._connection_rows[key] = d; r.addWidget(i); r.addWidget(n); r.addStretch(); r.addWidget(d); cv.addLayout(r)
        self._network_lbl = self._connection_rows["network"]; self._internet_lbl = self._connection_rows["internet"]; self._device_count_lbl = self._connection_rows["devices"]
        self._connection_rows["microphone"].setText("Bekleniyor"); self._connection_rows["speaker"].setText("Bekleniyor"); lay.addWidget(conn)
        section("Dosya Eki", "▣"); files = QFrame(); files.setObjectName("AttachmentPanel"); files.setStyleSheet(f"QFrame#AttachmentPanel {{ background:#0a2038; border:1px solid {C.BORDER}; border-radius:14px; }}")
        fv = QVBoxLayout(files); fv.setContentsMargins(12, 9, 12, 9); self._drop_zone = FileDropZone(); self._drop_zone.setFocusPolicy(Qt.FocusPolicy.StrongFocus); self._drop_zone.file_selected.connect(self._on_file_selected); self._file_hint = QLabel("Dosya seçin veya sürükleyin; burada onay işlemi yapılmaz."); self._file_hint.setWordWrap(True); self._file_hint.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;"); fv.addWidget(self._drop_zone); fv.addWidget(self._file_hint); lay.addWidget(files)
        section("Aktivite Akışı", "☷"); self._log = LogWidget(); self._log.setMinimumHeight(105); lay.addWidget(self._log, stretch=1)
        self._task_running_lbl = QLabel("RUNNING       --"); self._task_pending_lbl = QLabel("PENDING       --"); self._task_waiting_lbl = QLabel("APPROVAL      --"); self._task_completed_lbl = QLabel("COMPLETED     --")
        for label in (self._task_running_lbl, self._task_pending_lbl, self._task_waiting_lbl, self._task_completed_lbl): label.hide()
        self._interrupt_btn = QPushButton("□  JARVIS'İ DURDUR  [ESC]"); self._interrupt_btn.clicked.connect(self._do_interrupt); self._interrupt_btn.hide(); return w

    def _build_input_row(self):
        row = QWidget()
        outer = QVBoxLayout(row)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        self._composer_context_lbl = QLabel("")
        self._composer_context_lbl.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        self._composer_context_lbl.setStyleSheet(f"color:{C.TEXT_DIM}; background:transparent; border:none;")
        self._composer_context_lbl.hide()
        outer.addWidget(self._composer_context_lbl)

        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._composer_mute_btn = QPushButton("🎙")
        self._composer_mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._composer_mute_btn.clicked.connect(self._toggle_mute)
        lay.addWidget(self._composer_mute_btn)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Komutunuzu yazın veya konuşun...")
        self._input.setFont(QFont("Segoe UI", 10))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                color:{C.TEXT};
                background:#081c33;
                border:1px solid {C.BORDER};
                border-radius:16px;
                padding:12px 16px;
            }}
            QLineEdit:focus {{
                border:1px solid {C.PRI};
            }}
        """)

        self._input.returnPressed.connect(self._send)

        send = QPushButton("➤")
        self._send_btn = send
        send.setFont(QFont("Segoe UI Symbol", 15, QFont.Weight.Bold))
        send.setFixedSize(48, 48)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                color:#ffffff;
                background:{C.PRI};
                border:1px solid {C.PRI}; border-radius:21px;
                min-width:48px; max-width:48px;
            }}
            QPushButton:hover {{
                background:{C.ACC2};
                border:1px solid {C.ACC2};
            }}
        """)
        send.clicked.connect(self._send)

        lay.addWidget(self._input, stretch=1)
        lay.addWidget(send)
        outer.addLayout(lay)
        self._style_mute_btn()
        if not self._backend_enabled:
            self._composer_mute_btn.setEnabled(False)

        return row


    def _build_content_panel(self) -> QWidget:
        w = QFrame()
        w.setObjectName("ContentPanel")

        w.setStyleSheet(f"""
            QFrame#ContentPanel {{
                background:#02070c;
                border:1px solid {C.BORDER};
            }}
        """)

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(7)

        head = QHBoxLayout()

        self._content_title_lbl = QLabel("BRİFİNG")
        self._content_title_lbl.setFont(
            QFont("Courier New", 8, QFont.Weight.Bold)
        )
        self._content_title_lbl.setStyleSheet(
            f"color:{C.PRI}; background:transparent; border:none;"
        )

        head.addWidget(self._content_title_lbl)
        head.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(
            QFont("Courier New", 6)
        )
        self._content_ts_lbl.setStyleSheet(
            f"color:{C.TEXT_DIM}; background:transparent; border:none;"
        )
        head.addWidget(self._content_ts_lbl)

        close = QPushButton("×")
        close.setFixedSize(25, 22)
        close.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        close.setStyleSheet(f"""
            QPushButton {{
                color:{C.TEXT_DIM};
                background:transparent;
                border:1px solid {C.BORDER};
            }}
            QPushButton:hover {{
                color:{C.PRI};
                border:1px solid {C.PRI};
            }}
        """)
        close.clicked.connect(w.hide)

        head.addWidget(close)

        lay.addLayout(head)

        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(
            QFont("Courier New", 8)
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                color:{C.TEXT};
                background:#00070b;
                border:1px solid {C.BORDER};
                padding:8px;
            }}
        """)

        lay.addWidget(
            self._content_display,
            stretch=1
        )

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        self._content_panel.show()





    def _build_footer(self):
        w = QFrame()
        w.setObjectName("Footer")
        w.setFixedHeight(28)

        w.setStyleSheet(f"""
            QFrame#Footer {{
                background:#010609;
                border-top:1px solid {C.BORDER};
            }}
        """)

        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(16)

        for item in (
            "F4  MUTE",
            "ESC  INTERRUPT",
            "F11  FULLSCREEN",
        ):
            lbl = QLabel(item)
            lbl.setFont(
                QFont("Courier New", 6, QFont.Weight.Bold)
            )
            lbl.setStyleSheet(
                f"color:{C.TEXT_DIM}; background:transparent; border:none;"
            )
            lay.addWidget(lbl)

        lay.addStretch()

        version = QLabel("JARVIS V4.1")
        version.setFont(
            QFont("Courier New", 6, QFont.Weight.Bold)
        )
        version.setStyleSheet(
            f"color:{C.TEXT_DIM}; background:transparent; border:none;"
        )
        lay.addWidget(version)

        return w

    def _on_log(self, text: str):
        self._log.append_log(text)
        # Task flow is driven only by the persisted task records in
        # _refresh_task_center.  Log heuristics used to overwrite a newer
        # state with an older FILE_ANALYSIS/TASK message.
        if text.lower().startswith(("you:", "jarvis:")):
            speaker = "JARVIS" if text.lower().startswith("jarvis:") else "Sen"
            body = text.split(":", 1)[1].strip() if ":" in text else text
            if not body:
                return
            if hasattr(self, "_team_chat_lbl"):
                one_line = " ".join(body.split())
                preview = one_line[:112] + ("…" if len(one_line) > 112 else "")
                self._team_chat_lbl.setText(f"{speaker} · {preview}")
                self._network_canvas.update_runtime_status("history", "observed", f"Son konuşma: {speaker}")
            scroll_bar = self._chat_scroll.verticalScrollBar()
            follow_latest = scroll_bar.value() >= scroll_bar.maximum() - 4
            self._chat_empty_lbl.hide()
            self._chat_messages_layout.addWidget(self._bubble(speaker, body, speaker == "JARVIS"))
            if follow_latest:
                QTimer.singleShot(0, lambda bar=scroll_bar: bar.setValue(bar.maximum()))

    def _navigate(self, name: str):
        for nav_name, button in self._nav_buttons.items():
            active = nav_name == name
            button.setStyleSheet(
                f"QPushButton#NavButton {{ color:{C.WHITE if active else C.TEXT_MED}; "
                f"background:{'#0b2948' if active else 'transparent'}; border:none; "
                f"border-radius:9px; padding-left:5px; text-align:left; }} "
                f"QPushButton#NavButton:hover, QPushButton#NavButton:focus {{ color:{C.WHITE}; background:#0a223d; }}"
            )
        if name != "AI Ekip" and hasattr(self, "_lan_live_btn"):
            if self._lan_live_btn.isChecked() and self._lan_scan_worker is not None and self._lan_scan_worker.isRunning():
                self._lan_restart_after_finish = True
            if self._lan_live_btn.isChecked():
                self._lan_live_btn.setChecked(False)
            self._stop_lan_scan()
        network_view = name == "AI Ekip"
        self._right_panel.setVisible(not network_view)
        self._task_flow.setVisible(not network_view)
        if name == "Canlı Sohbet":
            self._chat_stack.setCurrentIndex(0)
        elif name == "Sistem":
            self._chat_stack.setCurrentIndex(1)
        elif name == "Görevler":
            self._chat_stack.setCurrentIndex(self._tasks_page_index)
        elif name == "AI Ekip":
            self._chat_stack.setCurrentIndex(self._team_page_index)
            self._set_team_mode(0)
            if not self._lan_live_btn.isChecked():
                self._lan_live_btn.setChecked(True)
        elif name == "Gelişim":
            self._chat_stack.setCurrentIndex(self._growth_page_index)
        elif name == "Dosyalar":
            self._chat_stack.setCurrentIndex(0)
            self._drop_zone.setFocus(Qt.FocusReason.OtherFocusReason)
            self._log_sig.emit("SYS: Dosya eki alanı odaklandı; bekleyen onay verisi yok.")
        elif name == "Ayarlar":
            self._chat_stack.setCurrentIndex(self._settings_page_index)
        else:
            self._log_sig.emit(f"SYS: {name} için backend görünümü bağlı değil.")

    def _on_file_selected(self, path: str):
        self._current_file = path
        p = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        try:
            size = _fmt_size(p.stat().st_size)
        except (OSError, ValueError) as exc:
            self._file_hint.setText(f"⚠  Dosya okunamadı: {p.name}")
            self._log_sig.emit(f"SYS: Dosya bilgisi okunamadı: {exc}")
            return
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell JARVIS what to do with it")
        self._task_status_lbl.setText("Görev hazır · Dosya analizi bekleniyor")
        self._set_task_stages("Planner")
        self._task_detail_lbl.setText(f"Mevcut görev: {p.name} · Komut bekleniyor")
        self._log_sig.emit(f"FILE: {p.name} ({size}) attached")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Uzak anahtar oluşturulamadı.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log_sig.emit("SYS: Mikrofon sessize alındı.")
        else:
            self._apply_state("LISTENING")
            self._log_sig.emit("SYS: Mikrofon aktif.")

    def _style_mute_btn(self):
        mic_name = str(self._mic_device_name or "").upper()
        unavailable = bool(mic_name.startswith(("YOK", "DEVRE KESİCİ", "HATA")))
        if not self._backend_enabled:
            left_text, icon, color, enabled = "MİKROFON · UI-ONLY", "🎙", C.TEXT_DIM, False
        elif unavailable:
            left_text, icon, color, enabled = "MİKROFON KULLANILAMIYOR", "🎙!", C.RED, False
        elif self._muted:
            left_text, icon, color, enabled = "MİKROFON SESSİZE ALINDI", "🔇", "#ffb44a", True
        else:
            left_text, icon, color, enabled = "SESSİZ MOD KAPALI", "🎙", C.GREEN, True
        if hasattr(self, "_mute_btn"):
            self._mute_btn.setText(f"{icon}  {left_text}")
            self._mute_btn.setEnabled(enabled)
            self._mute_btn.setStyleSheet(
                f"QPushButton {{ color:{color}; background:#07182b; border:1px solid {color}; border-radius:5px; }}"
                f"QPushButton:hover {{ background:#0a2038; }}"
            )
        if hasattr(self, "_composer_mute_btn"):
            self._composer_mute_btn.setText(icon)
            self._composer_mute_btn.setEnabled(enabled)
            self._composer_mute_btn.setToolTip(
                "Mikrofonu sessize al" if not self._muted else "Mikrofon sesini aç"
            )
            self._composer_mute_btn.setStyleSheet(
                f"QPushButton {{ color:{color}; background:#071a30; border:1px solid {color}; "
                "border-radius:24px; min-width:48px; max-width:48px; min-height:48px; max-height:48px; }"
            )

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log_sig.emit(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        state = str(state or "ERROR").upper(); self.hud.state = state; self.hud.speaking = state == "SPEAKING"
        if state in {"CONNECTING", "AUTH_REQUIRED", "ERROR", "LOCAL_ONLY", "LISTENING", "SPEAKING", "THINKING", "SLEEPING"}:
            self._connection_state = state
            self._touch_status("gemini")
            self._touch_status("system")
        elif state == "MUTED":
            self._touch_status("microphone")
            self._touch_status("system")
        network_state = {
            "CONNECTING": ("connecting", "Gemini Live oturumu deneniyor"),
            "AUTH_REQUIRED": ("auth_required", "API anahtarı doğrulaması gerekli"),
            "ERROR": ("error", "Canlı oturum hata durumunda"),
            "LOCAL_ONLY": ("disconnected", "UI-only modunda backend ve ses başlatılmadı"),
            "LISTENING": ("connected", "Canlı oturum açıldı"),
            "SPEAKING": ("connected", "Canlı oturum açık; yanıt seslendiriliyor"),
            "THINKING": ("connected", "Canlı oturum açık; araç çağrısı işleniyor"),
            "SLEEPING": ("disconnected", "Canlı oturum kapandı; yeniden bağlanma bekleniyor"),
        }.get(state)
        initial_placeholder = state == "CONNECTING" and getattr(self, "_network_initializing", False)
        if network_state and not initial_placeholder and hasattr(self, "_network_canvas"):
            self._network_canvas.update_runtime_status("session", *network_state)
            self._network_canvas.update_runtime_status("main", *network_state)
            self._show_network_node_details(self._network_canvas.selected_node)
        if hasattr(self, "_waveform"): self._waveform.set_active(state == "SPEAKING")
        labels = {"CONNECTING":"◌  Bağlanıyor", "AUTH_REQUIRED":"⚠  Kimlik doğrulama gerekli", "ERROR":"⚠  Hata", "LISTENING":"●  Dinliyor", "SPEAKING":"◉  Yanıt veriyor", "SLEEPING":"○  Beklemede", "MUTED":"◌  Mikrofon sessiz", "LOCAL_ONLY":"○  Yerel arayüz · ses kapalı"}
        text = labels.get(state, f"●  {state.title()}")
        voice_state = {
            "SPEAKING": ("SPEAKING", "Yanıt seslendiriliyor"),
            "THINKING": ("THINKING", "Agent Loop çalışıyor"),
            "LISTENING": ("LISTENING", "Mikrofon hazır"),
            "SLEEPING": ("SLEEPING", "Bekleme modu"),
            "MUTED": ("MUTED", "Mikrofon kapalı"),
            "ERROR": ("ERROR", "Ses bağlantısı kontrol edilmeli"),
            "LOCAL_ONLY": ("SLEEPING", "UI-only modunda mikrofon ve ses başlatılmadı"),
        }.get(state, ("LISTENING", "Yerel UI hazır"))
        if hasattr(self, "_voice_hud"):
            self._voice_hud.set_state(*voice_state)
        if hasattr(self, "_center_state_lbl"): self._center_state_lbl.setText(text)
        if hasattr(self, "_chat_meta_lbl"):
            if state in {"LISTENING", "SPEAKING", "THINKING"}:
                self._chat_meta_lbl.setText("Backend bağlı · görev verisi canlı")
            elif state == "LOCAL_ONLY":
                self._chat_meta_lbl.setText("UI-only mod · backend ve ses kapalı")
            elif state == "AUTH_REQUIRED":
                self._chat_meta_lbl.setText("Backend kimlik doğrulaması bekleniyor")
            elif state == "CONNECTING":
                self._chat_meta_lbl.setText("Backend bağlantısı kuruluyor…")
            else:
                self._chat_meta_lbl.setText("Backend bağlantısı bekleniyor")
        if hasattr(self, "_internet_lbl"):
            online = state in {"LISTENING", "SPEAKING", "THINKING"}
            text = "Kapalı (UI-only)" if state == "LOCAL_ONLY" else ("Gemini bağlı" if online else "Bekleniyor")
            self._internet_lbl.setText(text)
            self._internet_lbl.setStyleSheet(
                f"color:{C.GREEN if online else C.TEXT_DIM}; background:transparent; border:none;"
            )
        if hasattr(self, "_health_lbl"):
            color = C.TEXT_DIM if state == "LOCAL_ONLY" else (C.MUTED_C if state == "MUTED" else (C.RED if state == "ERROR" else (C.PRI if state in {"CONNECTING", "AUTH_REQUIRED"} else C.GREEN)))
            self._health_lbl.setText(f"{text}   ·   UI yerel"); self._health_lbl.setStyleSheet(f"color:{color}; background:#071c32; border:1px solid {C.BORDER}; border-radius:9px; padding-left:10px;")
        self._refresh_header_statuses()

    def _apply_voice_state(self, state: str, detail: str):
        if hasattr(self, "_voice_hud"):
            self._voice_hud.set_state(state, detail)

    def _apply_voice_transcript(self, text: str):
        if hasattr(self, "_voice_hud"):
            self._voice_hud.set_transcript(text)

    def _apply_voice_volume(self, value: float):
        if hasattr(self, "_voice_hud"):
            self._voice_hud.set_volume(value)

    def _on_mic_device(self, name: str):
        self._mic_device_name = str(name or "").strip()
        self._touch_status("microphone")
        self._touch_status("system")
        upper = self._mic_device_name.upper()
        unavailable = upper.startswith(("YOK", "DEVRE KESİCİ", "HATA"))
        unknown = not self._mic_device_name or upper in {"UNKNOWN", "BILINMIYOR", "BİLİNMİYOR", "N/A"}
        shown = "Kullanılamıyor" if unavailable else ("Bilinmiyor" if unknown else self._mic_device_name)
        self._mic_lbl.setText(f"🎤  Mikrofon: {shown}")
        if hasattr(self, "_connection_rows"):
            self._connection_rows["microphone"].setText("Kullanılamıyor" if unavailable else ("Bilinmiyor" if unknown else "Hazır"))
        self._style_mute_btn()
        self._refresh_header_statuses()

    def _on_speaker_device(self, name: str):
        self._speaker_device_name = str(name or "").strip()
        self._touch_status("speaker")
        self._touch_status("system")
        upper = self._speaker_device_name.upper()
        unavailable = upper.startswith(("YOK", "HATA"))
        unknown = not self._speaker_device_name or upper in {"UNKNOWN", "BILINMIYOR", "BİLİNMİYOR", "N/A"}
        shown = "Kullanılamıyor" if unavailable else ("Bilinmiyor" if unknown else self._speaker_device_name)
        self._speaker_lbl.setText(f"🔊  Hoparlör: {shown}")
        if hasattr(self, "_connection_rows"):
            self._connection_rows["speaker"].setText(
                "Kullanılamıyor" if unavailable else ("Bilinmiyor" if unknown else "Hazır")
            )
        self._refresh_header_statuses()

    def _check_config(self) -> bool:
        try:
            get_gemini_api_key()
            return True
        except (RuntimeError, OSError, ValueError):
            return False

    def _show_setup(self):
        if self._overlay is None:
            self._overlay = SetupOverlay(self.centralWidget()); self._overlay.done.connect(self._on_setup_done); self._overlay.dismissed.connect(self._dismiss_setup)
        cw = self.centralWidget(); self._overlay.setGeometry((cw.width() - 460) // 2, (cw.height() - 430) // 2, 460, 430); self._overlay.show(); self._overlay.raise_()

    def _dismiss_setup(self):
        if self._overlay: self._overlay.hide()
        self._apply_state("AUTH_REQUIRED"); self._log_sig.emit("SYS: Yerel UI devam ediyor; API anahtarı ayarlanmadı.")

    def _on_setup_done(self, key: str, os_name: str):
        # A stale GEMINI_API_KEY inherited by the parent PowerShell process
        # must not override the key the user has just entered in this UI.
        # Keep the process-local override; persistent storage remains managed
        # by secure_config and no secret is logged.
        key = str(key or "").strip()
        if key:
            os.environ["GEMINI_API_KEY"] = key
        data = load_config(); data.update({"gemini_api_key": key, "os_system": os_name}); save_config(data)
        self._ready = True
        if self._overlay: self._overlay.hide()
        self._apply_state("CONNECTING"); self._log_sig.emit(f"SYS: Anahtar varlığı kaydedildi. OS={os_name.upper()}; remote doğrulama bekleniyor.")

    def closeEvent(self, event):
        if hasattr(self, "_lan_live_btn") and self._lan_live_btn.isChecked():
            self._lan_live_btn.setChecked(False)
        if hasattr(self, "_lan_refresh_tmr"):
            self._stop_lan_scan(wait=True)
        self.stop_camera_stream()
        self._clock_tmr.stop()
        self._metric_tmr.stop()
        self._task_tmr.stop()
        self._cam_preview._timer.stop()
        self._drop_zone._anim_tmr.stop()
        self._log._tmr.stop()
        self.hud._tmr.stop()
        self._waveform.stop()
        self._voice_hud._timer.stop()
        if self._overlay: self._overlay.hide()
        if self._remote_overlay: self._remote_overlay._do_close()
        if self._cam_thread and self._cam_thread.is_alive():
            self._cam_thread.join(timeout=0.5)
        global _metrics
        if _metrics is not None: _metrics.stop()
        super().closeEvent(event)

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None, *, backend_enabled: bool = True):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path, backend_enabled=backend_enabled)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def set_network_status(self, component: str, status: str, detail: str = ""):
        """Thread-safe update for a runtime state observed by the app."""
        self._win._network_status_sig.emit(str(component), str(status), str(detail))

    def set_network_tool_status(self, tool_name: str, status: str):
        """Thread-safe update for a tool dispatch observed by JarvisLive."""
        self._win._network_status_sig.emit("tool", str(status), str(tool_name))

    def set_voice_state(self, state: str, detail: str = ""):
        """Thread-safe update for the compact Voice Assistant HUD."""
        self._win._voice_state_sig.emit(str(state), str(detail))

    def set_voice_transcript(self, text: str):
        """Thread-safe partial transcript update for the compact HUD."""
        self._win._voice_transcript_sig.emit(str(text or ""))

    def set_voice_volume(self, value: float):
        """Thread-safe normalized RMS update for the compact HUD."""
        try:
            self._win._voice_volume_sig.emit(float(value))
        except (TypeError, ValueError):
            pass

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def set_mic_device(self, name: str):
        """Thread-safe: sol panelde aktif mikrofon adini gunceller."""
        self._win._mic_dev_sig.emit(name)

    def set_speaker_device(self, name: str):
        """Thread-safe: sol panelde aktif hoparlor/kulaklik adini gunceller."""
        self._win._speaker_dev_sig.emit(name)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
