import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_CNW: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_os() -> str:
    """Önce GERÇEK işletim sistemi (Murat@goxs 2026-09-30: Ubuntu'da ayar dosyasında
    'windows' kaldığı için Windows'un schtasks aracı çağrıldı, hatırlatıcılar hiç
    kurulamadı). Ayar yalnız sistem tanınmazsa kullanılır."""
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "mac"
    if system == "Windows":
        return "windows"
    try:
        cfg = json.loads(
            (_base_dir() / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
        return cfg.get("os_system", "linux").lower()
    except Exception:
        return "linux"


_DAY_ALIASES = {
    "daily": "*", "her gün": "*", "hergün": "*", "every day": "*",
    "weekdays": "Mon..Fri", "hafta içi": "Mon..Fri", "haftaiçi": "Mon..Fri",
    "weekends": "Sat,Sun", "hafta sonu": "Sat,Sun",
}
_DAY_NAMES = {
    "mon": "Mon", "pzt": "Mon", "pazartesi": "Mon", "tue": "Tue", "sal": "Tue", "salı": "Tue",
    "wed": "Wed", "çar": "Wed", "çarşamba": "Wed", "thu": "Thu", "per": "Thu", "perşembe": "Thu",
    "fri": "Fri", "cum": "Fri", "cuma": "Fri", "sat": "Sat", "cmt": "Sat", "cumartesi": "Sat",
    "sun": "Sun", "paz": "Sun", "pazar": "Sun",
}


def repeat_to_calendar(repeat: str, time_str: str) -> str | None:
    """'weekdays' + '17:00' → 'Mon..Fri *-*-* 17:00:00' (systemd OnCalendar). Anlaşılmazsa None."""
    rep = (repeat or "").strip().lower()
    if not rep:
        return None
    days = _DAY_ALIASES.get(rep)
    if days is None:
        parts = [x.strip() for x in rep.replace(";", ",").replace(" ", ",").split(",") if x.strip()]
        mapped = [_DAY_NAMES.get(x) for x in parts]
        if not parts or None in mapped:
            return None
        days = ",".join(dict.fromkeys(mapped))
    hh, mm = time_str.split(":")[:2]
    prefix = "" if days == "*" else days + " "
    return f"{prefix}*-*-* {int(hh):02d}:{int(mm):02d}:00"


def _scripts_dir() -> Path:
    d = Path.home() / ".jarvis" / "reminders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sanitise(text: str, max_len: int = 200) -> str:
    return (
        text.replace("\\", "")
            .replace('"', "")
            .replace("'", "")
            .replace("\n", " ")
            .replace("\r", "")
            .strip()
    )[:max_len]

_SELF_DELETE = """# Self-delete after firing
try:
    pathlib.Path(__file__).unlink(missing_ok=True)
except Exception:
    pass
"""


def _write_notify_script(task_name: str, message: str, os_name: str, keep: bool = False) -> Path:
    script_path = _scripts_dir() / f"{task_name}.py"
    msg_literal = json.dumps(message)  

    if os_name == "windows":
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast("J.A.R.V.I.S Reminder", message, duration=15, threaded=False)
        notified = True
    except Exception:
        pass

if not notified:
    try:
        import subprocess
        subprocess.run(["msg", "*", "/TIME:30", message], check=False)
    except Exception:
        pass

try:
    import winsound
    for freq in [800, 1000, 1200]:
        winsound.Beep(freq, 180)
        import time; time.sleep(0.08)
except Exception:
    pass
"""

    elif os_name == "mac":
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        import subprocess
        script = 'display notification "{{}}" with title "J.A.R.V.I.S Reminder"'.format(
            message.replace('"', '')
        )
        subprocess.run(["osascript", "-e", script], check=False)
    except Exception:
        pass
"""

    else:  # linux
        notify_block = f"""
message = {msg_literal}
notified = False

try:
    from plyer import notification
    notification.notify(title="J.A.R.V.I.S Reminder", message=message, timeout=15)
    notified = True
except Exception:
    pass

if not notified:
    try:
        import subprocess
        subprocess.run(
            ["notify-send", "--urgency=critical", "--expire-time=60000",
             "J.A.R.V.I.S Hatırlatma", message],
            check=False
        )
    except Exception:
        pass
try:
    import subprocess, pathlib as _p
    _snd = _p.Path("/usr/share/sounds/freedesktop/stereo/complete.oga")
    if _snd.is_file():
        subprocess.run(["paplay", str(_snd)], check=False)
except Exception:
    pass
"""

    script_body = f"""# Auto-generated by J.A.R.V.I.S reminder — do not edit
import sys, os, pathlib
{notify_block}
{"" if keep else _SELF_DELETE}"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o600)   # owner read/write only
    return script_path

def _schedule_windows(target_dt: datetime, task_name: str,
                      script_path: Path, message: str) -> str:
    python_exe = Path(sys.executable)
    pythonw = python_exe.parent / "pythonw.exe"
    if pythonw.exists():
        python_exe = pythonw

    xml_path = _scripts_dir() / f"{task_name}.xml"
    xml_content = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>J.A.R.V.I.S Reminder</Description></RegistrationInfo>\n'
        '  <Triggers><TimeTrigger>\n'
        f'    <StartBoundary>{target_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>\n'
        '    <Enabled>true</Enabled>\n'
        '  </TimeTrigger></Triggers>\n'
        '  <Actions><Exec>\n'
        f'    <Command>{python_exe}</Command>\n'
        f'    <Arguments>"{script_path}"</Arguments>\n'
        '  </Exec></Actions>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n'
        '    <Enabled>true</Enabled>\n'
        '  </Settings>\n'
        '  <Principals><Principal>\n'
        '    <LogonType>InteractiveToken</LogonType>\n'
        '    <RunLevel>LeastPrivilege</RunLevel>\n'
        '  </Principal></Principals>\n'
        '</Task>'
    )

    xml_path.write_text(xml_content, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
        capture_output=True, text=True, **_CNW,
    )

    try:
        xml_path.unlink(missing_ok=True)
    except Exception:
        pass

    if result.returncode != 0:
        script_path.unlink(missing_ok=True)
        err = (result.stderr or result.stdout).strip()
        print(f"[Reminder] ❌ schtasks: {err}")
        return ""  

    return task_name


def _schedule_mac(target_dt: datetime, task_name: str,
                  script_path: Path) -> str:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    label     = f"com.jarvis.reminder.{task_name}"
    plist_path = agents_dir / f"{label}.plist"

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script_path}</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Year</key>   <integer>{target_dt.year}</integer>
    <key>Month</key>  <integer>{target_dt.month}</integer>
    <key>Day</key>    <integer>{target_dt.day}</integer>
    <key>Hour</key>   <integer>{target_dt.hour}</integer>
    <key>Minute</key> <integer>{target_dt.minute}</integer>
  </dict>
  <key>RunAtLoad</key>         <false/>
  <key>StandardOutPath</key>   <string>/dev/null</string>
  <key>StandardErrorPath</key> <string>/dev/null</string>
</dict>
</plist>
"""
    plist_path.write_text(plist_content, encoding="utf-8")
    plist_path.chmod(0o644)

    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        plist_path.unlink(missing_ok=True)
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ launchctl: {result.stderr.strip()}")
        return ""

    return label


def _schedule_linux(target_dt: datetime, task_name: str,
                    script_path: Path) -> str:

    if shutil.which("systemd-run"):
        on_calendar = target_dt.strftime("%Y-%m-%d %H:%M:00")
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                f"--on-calendar={on_calendar}",
                f"--unit={task_name}",
                "--",
                sys.executable, str(script_path),
            ],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return task_name
        print(f"[Reminder] ⚠️ systemd-run failed: {result.stderr.strip()}, trying 'at'")

    if shutil.which("at"):
        at_time = target_dt.strftime("%H:%M %Y-%m-%d")
        cmd_str = f"{sys.executable} {script_path}\n"
        result  = subprocess.run(
            ["at", at_time],
            input=cmd_str, capture_output=True, text=True,
        )
        if result.returncode == 0:
            return task_name
        print(f"[Reminder] ❌ at: {result.stderr.strip()}")
        return ""

    print("[Reminder] ❌ Neither systemd-run nor at found on this Linux system.")
    return ""

def _systemd_user_dir() -> Path:
    d = Path.home() / ".config" / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _schedule_linux_repeating(calendar: str, task_name: str, script_path: Path, message: str) -> str:
    """Tekrarlayan hatırlatma: bilgisayar yeniden başlasa da kalıcı kullanıcı zamanlayıcısı
    (~/.config/systemd/user/<ad>.timer). Kaldırmak: systemctl --user disable --now <ad>.timer"""
    if not shutil.which("systemctl"):
        print("[Reminder] ❌ systemctl yok; tekrarlayan hatırlatma kurulamadı.")
        return ""
    unit_dir = _systemd_user_dir()
    (unit_dir / f"{task_name}.service").write_text(
        f"[Unit]\nDescription=JARVIS hatirlatma: {message[:60]}\n\n"
        f"[Service]\nType=oneshot\nExecStart={sys.executable} {script_path}\n",
        encoding="utf-8")
    (unit_dir / f"{task_name}.timer").write_text(
        f"[Unit]\nDescription=JARVIS hatirlatma zamanlayicisi: {message[:60]}\n\n"
        f"[Timer]\nOnCalendar={calendar}\nPersistent=false\n\n[Install]\nWantedBy=timers.target\n",
        encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    result = subprocess.run(["systemctl", "--user", "enable", "--now", f"{task_name}.timer"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[Reminder] ❌ systemctl: {result.stderr.strip()}")
        return ""
    return task_name


def list_repeating() -> list[str]:
    d = Path.home() / ".config" / "systemd" / "user"
    return sorted(p.stem for p in d.glob("JARVISReminder_*.timer")) if d.is_dir() else []


def reminder(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:

    date_str = parameters.get("date", "").strip()
    time_str = parameters.get("time", "").strip()
    message  = parameters.get("message", "Reminder").strip()
    repeat   = str(parameters.get("repeat", "") or "").strip()

    if repeat:
        # Tekrarlayan hatırlatma (ör. hafta içi her gün 17:00).
        if not time_str:
            return "Tekrarlayan hatırlatma için saat gerekli (HH:MM)."
        try:
            calendar = repeat_to_calendar(repeat, time_str)
        except ValueError:
            calendar = None
        if not calendar:
            return ("Tekrar biçimini anlayamadım. 'daily', 'weekdays', 'weekends' ya da "
                    "'mon,wed,fri' gibi gün listesi kullan.")
        os_name = _get_os()
        if os_name != "linux":
            return "Tekrarlayan hatırlatma şimdilik yalnız Linux'ta destekleniyor."
        safe_msg = _sanitise(message)
        task_name = f"JARVISReminder_{datetime.now().strftime('%Y%m%d_%H%M%S')}_tekrar"
        script_path = _write_notify_script(task_name, safe_msg, os_name, keep=True)
        job = _schedule_linux_repeating(calendar, task_name, script_path, safe_msg)
        if not job:
            script_path.unlink(missing_ok=True)
            return "Tekrarlayan hatırlatmayı sisteme kaydedemedim."
        if player:
            player.write_log(f"[Reminder] ✅ {repeat} {time_str} — {safe_msg[:40]}")
        return (f"Tekrarlayan hatırlatma kuruldu: {repeat} {time_str} — '{safe_msg}'. "
                f"Bilgisayar yeniden başlasa da çalışır (ad: {task_name}).")

    if not date_str or not time_str:
        return "I need both a date and a time to set a reminder."

    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "I couldn't parse that date or time. Please use YYYY-MM-DD and HH:MM."

    if target_dt <= datetime.now():
        return "That time has already passed — I can't set a reminder in the past."

    os_name    = _get_os()
    safe_msg   = _sanitise(message)
    task_name  = f"JARVISReminder_{target_dt.strftime('%Y%m%d_%H%M%S')}"

    try:
        script_path = _write_notify_script(task_name, safe_msg, os_name)
    except Exception as e:
        return f"Could not prepare the reminder script: {e}"

    try:
        if os_name == "windows":
            job_id = _schedule_windows(target_dt, task_name, script_path, safe_msg)
        elif os_name == "mac":
            job_id = _schedule_mac(target_dt, task_name, script_path)
        else:
            job_id = _schedule_linux(target_dt, task_name, script_path)
    except Exception as e:
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ Scheduling exception: {e}")
        return "Something went wrong while scheduling the reminder."

    if not job_id:
        return "I couldn't register the reminder with the system scheduler."

    if player:
        player.write_log(f"[Reminder] ✅ {date_str} {time_str} — {safe_msg[:40]}")

    friendly_time = target_dt.strftime("%B %d at %I:%M %p")
    return f"Reminder set for {friendly_time}."