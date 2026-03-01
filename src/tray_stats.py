# -*- coding: utf-8 -*-
"""
Трей: подсказка — CPU, ядра, GPU, ОЗУ, диски, сеть. Меню: Показать показатели (полный список), Выключить.
"""
import json
import os
import queue
import re
import sys
import subprocess
import threading
import time

from PIL import Image

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pystray
except ImportError:
    pystray = None

# Подсказка в трее Windows — не длиннее 127 символов (szTip = 128)
TOOLTIP_MAX = 127
# Чтобы при сборке в .exe дочерние процессы не показывали консоль
SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Кэш для скорости сети (время, bytes_recv, bytes_sent)
_net_last = (0.0, 0, 0)
# Кэш «тяжёлых» показателей: TTL 3 с, формат {key: (timestamp, value)}
_HEAVY_CACHE_TTL = 3.0
_heavy_cache = {}
_heavy_cache_lock = threading.Lock()
# Полный список ядер для окна (64), обновляется реже
_cores_full_cache = [0.0, ""]

# Настройки только для всплывающей подсказки в трее (при наведении на иконку)
TRAY_VISIBLE = {"cpu": True, "cores": True, "gpu": True, "ram": True, "disks": True, "network": True}
TRAY_INTERVAL = 2  # секунд между обновлениями подсказки в трее
# Готовая строка подсказки (считается в фоне, в потоке трея только подставляется — иначе цикл сообщений блокируется)
TRAY_LAST_TOOLTIP = ""

# Общий кэш сбора статистики: обновляется по TRAY_INTERVAL и по кнопке «Обновить»
STATS_CACHE = {}
STATS_CACHE_LOCK = threading.Lock()

# Запись статистики в файл: вкл/выкл. При включении пишется при каждом обновлении кэша (по настройке интервала).
STATS_RECORDING = False

# Окно показателей: ссылка на root и флаг «закрыть по двойному клику»
_stats_window_root = None
_stats_window_close_requested = False
# Очередь запросов «Показать показатели» из потока трея (Tk нельзя вызывать не из главного потока)
_tray_activate_queue = queue.Queue()

# Вывод статистики на экран (оверлей)
OVERLAY_ENABLED = False
OVERLAY_TEXT_COLOR = "#ffffff"
OVERLAY_POSITION = "top_left"  # top_left, top_right, bottom_left, bottom_right
OVERLAY_BACKGROUND = "transparent"  # transparent, black_25, black_50, white_25, white_50
OVERLAY_BOLD = False
_overlay_root = None
_overlay_thread = None

# Варианты для выпадающих списков оверлея (подписи из lang через t())
OVERLAY_COLOR_KEYS = [("white", "#ffffff"), ("yellow", "#ffff00"), ("green", "#00ff00"), ("blue", "#89b4fa"), ("red", "#ff6666"), ("black", "#000000")]
OVERLAY_POSITION_KEYS = [("topleft", "top_left"), ("topright", "top_right"), ("bottomleft", "bottom_left"), ("bottomright", "bottom_right")]
OVERLAY_BG_KEYS = [("transparent", "transparent"), ("black25", "black_25"), ("black50", "black_50"), ("white25", "white_25"), ("white50", "white_50")]

def _overlay_colors():
    return [(t("overlay_color_" + k), v) for k, v in OVERLAY_COLOR_KEYS]

def _overlay_positions():
    return [(t("overlay_pos_" + k), v) for k, v in OVERLAY_POSITION_KEYS]

def _overlay_backgrounds():
    return [(t("overlay_bg_" + k), v) for k, v in OVERLAY_BG_KEYS]

# Метаданные программы (подпись внизу окна)
APP_NAME = "tray_stats"
APP_AUTHOR = "sir_rumata"
APP_VERSION = "β.0.5.5"
APP_YEAR = "2026"

# Иконка трея и окна — готовый файл в корне (рядом с run.py / exe)
TRAY_ICON_FILENAME = "tray_stats.ico"
def _base_path():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_path():
    return os.path.join(_base_path(), "tray_stats_settings.json")


def _lang_dir():
    """Каталог с языковыми файлами (src/lang или рядом с exe при сборке)."""
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return os.path.join(sys._MEIPASS, "lang")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")


# Текущий языковой пакет (ключ → строка)
_LANG = {}


def load_lang(code):
    """Загрузить языковой пакет: rus, eng, bel, tat, chi. Fallback — rus."""
    global _LANG
    code = (code or "rus").strip().lower()
    if code not in ("rus", "eng", "bel", "tat", "chi"):
        code = "rus"
    path = os.path.join(_lang_dir(), code + ".json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                _LANG = json.load(f)
                return
    except Exception:
        pass
    # Fallback на русский
    path_rus = os.path.join(_lang_dir(), "rus.json")
    try:
        if os.path.isfile(path_rus):
            with open(path_rus, "r", encoding="utf-8") as f:
                _LANG = json.load(f)
        else:
            _LANG = {}
    except Exception:
        _LANG = {}


def t(key):
    """Строка по ключу из текущего языкового пакета."""
    return _LANG.get(key, key)


def _append_stats_to_file():
    """Дописать в tray_stats_log.txt одну порцию: дата/время, строки статистики, пустая строка."""
    try:
        from datetime import datetime
        lines = get_full_stats_lines(max_cores=64, visible=None)
        if not lines:
            return
        log_path = os.path.join(_base_path(), "tray_stats_log.txt")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            f.write("\n".join(lines) + "\n\n")
    except Exception:
        pass


def load_settings():
    """Загружает настройки из JSON. Возвращает dict: tray_visible, tray_interval, autostart."""
    try:
        p = _config_path()
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _get_system_theme():
    """Тема системы: 'dark' или 'light'. Windows 10/11 — реестр Personalize\\AppsUseLightTheme."""
    if sys.platform != "win32":
        return "light"
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            0, winreg.KEY_READ,
        )
        try:
            use_light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
            return "light" if use_light else "dark"
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    return "light"


def save_settings(tray_visible=None, tray_interval=None, autostart=None,
                  overlay_enabled=None, overlay_text_color=None, overlay_position=None, overlay_background=None,
                  overlay_bold=None, theme=None, lang=None):
    """Сохраняет настройки. Переданные None не перезаписывают текущие в файле."""
    try:
        p = _config_path()
        data = load_settings()
        if tray_visible is not None:
            data["tray_visible"] = tray_visible
        if tray_interval is not None:
            data["tray_interval"] = tray_interval
        if autostart is not None:
            data["autostart"] = autostart
        if overlay_enabled is not None:
            data["overlay_enabled"] = overlay_enabled
        if overlay_text_color is not None:
            data["overlay_text_color"] = overlay_text_color
        if overlay_position is not None:
            data["overlay_position"] = overlay_position
        if overlay_background is not None:
            data["overlay_background"] = overlay_background
        if overlay_bold is not None:
            data["overlay_bold"] = overlay_bold
        if theme is not None:
            data["theme"] = theme
        if lang is not None:
            data["lang"] = lang
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def set_autostart_windows(enable):
    """Включить/выключить автозапуск с Windows (реестр HKCU\\...\\Run)."""
    if sys.platform != "win32":
        return
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "SystemStatsTray"
        if getattr(sys, "frozen", False):
            # Путь в кавычках — иначе путь с пробелами (например C:\Program Files\...) не запустится
            cmd = f'"{sys.executable}"'
        else:
            run_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
            cmd = f'"{sys.executable}" "{run_py}"'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
        try:
            if enable:
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass


def _get_cpu(interval=0):
    if not psutil:
        return "—"
    try:
        return f"{psutil.cpu_percent(interval=interval):.0f}%"
    except Exception:
        return "—"


def _get_cpu_temp_mahm():
    """Температура CPU из общей памяти MSI Afterburner (MAHM), если он запущен. Windows only."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        # 64-bit: MapViewOfFile возвращает 64-битный указатель — без restype будет обрезка и 0xC0000005
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_size_t,
        ]
        FILE_MAP_READ = 0x0004
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        h = kernel32.OpenFileMappingW(
            wintypes.DWORD(FILE_MAP_READ),
            wintypes.BOOL(False),
            "MAHMSharedMemory",
        )
        if not h or ctypes.c_void_p(h).value == INVALID_HANDLE_VALUE:
            return ""
        try:
            addr = kernel32.MapViewOfFile(
                wintypes.HANDLE(h),
                wintypes.DWORD(FILE_MAP_READ),
                0, 0, 0,
            )
            if not addr:
                return ""
            addr = addr if isinstance(addr, int) else ctypes.c_void_p(addr).value
            try:
                buf = (ctypes.c_char * 32).from_address(addr)
                sig = int.from_bytes(buf[:4], "little")
                if sig != 0x4D41484D:  # 'MAHM'
                    return ""
                num_entries = int.from_bytes(buf[12:16], "little")
                entry_size = int.from_bytes(buf[16:20], "little")
                # Жёсткие лимиты — не читать за границы отображения
                if num_entries <= 0 or num_entries > 512:
                    return ""
                if entry_size < 1324 or entry_size > 2048:
                    return ""
                total_size = 32 + num_entries * entry_size
                if total_size > 5 * 1024 * 1024:
                    return ""
                ENTRY_OFF_DATA = 5 * 260
                ENTRY_OFF_SRC_ID = ENTRY_OFF_DATA + 3 * 4 + 2 * 4
                CPU_TEMP_ID = 0x80
                offset = 32
                for _ in range(num_entries):
                    entry_start = addr + offset
                    src_id = int.from_bytes(
                        (ctypes.c_char * 4).from_address(entry_start + ENTRY_OFF_SRC_ID)[:4],
                        "little",
                    )
                    if src_id == CPU_TEMP_ID:
                        data_val = (ctypes.c_float).from_address(entry_start + ENTRY_OFF_DATA).value
                        if 0 <= data_val < 200:
                            return f"{int(round(data_val))}"
                        break
                    offset += entry_size
            finally:
                kernel32.UnmapViewOfFile(ctypes.c_void_p(addr))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(h))
    except Exception:
        pass
    return ""


def _get_cpu_temp():
    """Температура CPU (°C) или пусто. Windows: MAHM (Afterburner), WMI, Libre Hardware Monitor. С кэшем 3 с."""
    key = "cpu_temp"
    now = time.time()
    with _heavy_cache_lock:
        if key in _heavy_cache:
            ts, val = _heavy_cache[key]
            if now - ts < _HEAVY_CACHE_TTL:
                return val
    val = _get_cpu_temp_impl()
    with _heavy_cache_lock:
        _heavy_cache[key] = (now, val)
    return val


def _get_cpu_temp_impl():
    """Реальная реализация сбора температуры CPU."""
    if not psutil:
        return ""
    try:
        st = getattr(psutil, "sensors_temperature", None)
        if st:
            raw = st()
            for name, entries in (raw or {}).items():
                for e in entries:
                    if getattr(e, "current", None) is not None:
                        return f"{e.current:.0f}"
        if sys.platform == "win32":
            # 0) MSI Afterburner / MAHM shared memory (если запущен — та же температура, что в нём)
            t = _get_cpu_temp_mahm()
            if t:
                return t
            # 1) Стандартный WMI (работает не на всех ПК)
            for cmd in [
                "Get-CimInstance -Namespace root/WMI -ClassName MSAcpi_ThermalZoneTemperature -EA 0 | ForEach-Object { [int](($_.CurrentTemperature/10)-273.15) }",
                "Get-WmiObject -Namespace root/WMI -Class MSAcpi_ThermalZoneTemperature -EA 0 | ForEach-Object { [int](($_.CurrentTemperature/10)-273.15) }",
            ]:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", cmd],
                    capture_output=True, text=True, timeout=2,
                    creationflags=SUBPROCESS_FLAGS,
                )
                if r.returncode == 0 and r.stdout.strip():
                    v = r.stdout.strip().split()[0]
                    if v.lstrip("-").isdigit():
                        return v
            # 2) Libre Hardware Monitor — если установлен и запущен, отдаёт температуру через WMI
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -EA 0 | Where-Object { $_.SensorType -eq 1 -and $_.Name -match 'Core|Package|CPU' } | Select-Object -First 1 -ExpandProperty Value"],
                capture_output=True, text=True, timeout=2,
                creationflags=SUBPROCESS_FLAGS,
            )
            if r.returncode == 0 and r.stdout.strip():
                v = r.stdout.strip().split()[0].replace(",", ".")
                try:
                    return f"{float(v):.0f}"
                except ValueError:
                    pass
    except Exception:
        pass
    return ""


def _get_cores(max_cores=8):
    """ЯДРА: 1: 10% | 2: 10% | ... (max_cores для подсказки, в полном окне — все)."""
    if not psutil:
        return "—"
    try:
        per = psutil.cpu_percent(interval=0.1, percpu=True)
        return " | ".join(f"{i+1}: {p:.0f}%" for i, p in enumerate(per[:max_cores]))
    except Exception:
        return "—"


def _get_cores_full_cached():
    """Полный список ядер (64) для окна показателей, с кэшем 5 с."""
    now = time.time()
    if _cores_full_cache[0] and now - _cores_full_cache[0] < 5:
        return _cores_full_cache[1]
    val = _get_cores(max_cores=64)
    _cores_full_cache[0] = now
    _cores_full_cache[1] = val
    return val


def _get_ram():
    """ОЗУ: used_mb/total_mb"""
    if not psutil:
        return "—"
    try:
        v = psutil.virtual_memory()
        used_mb = v.used // (1024 * 1024)
        total_mb = v.total // (1024 * 1024)
        return f"{used_mb} / {total_mb}"
    except Exception:
        return "—"


def _disk_label(mountpoint):
    """C:\\ или C: -> C:"""
    if not mountpoint:
        return "?"
    s = str(mountpoint).strip().rstrip("\\/")
    if not s:
        return "?"
    # C:\ или C: или /mnt/c
    if len(s) >= 2 and s[1] == ":":
        return s[0].upper() + ":"
    if s.startswith("/") and "/" in s[1:]:
        return s.split("/")[-1][:2] or "?"
    return s[:2] if len(s) >= 2 else s


def _get_disks_win32_ctypes(max_parts=4):
    """ДИСКИ на Windows: GetLogicalDriveStringsW + перебор A–Z через GetDiskFreeSpaceExW."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        total_bytes = ctypes.c_ulonglong()
        free_bytes = ctypes.c_ulonglong()
        parts = []
        seen = set()

        def add_drive(letter):
            if max_parts is not None and len(parts) >= max_parts:
                return
            if letter in seen:
                return
            path = letter + ":\\"
            if kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), None, ctypes.byref(total_bytes), ctypes.byref(free_bytes)):
                total = total_bytes.value
                if total > 0:
                    used = total - free_bytes.value
                    pct = int(round(used * 100.0 / total))
                    parts.append(f"{letter}: {pct}%")
                    seen.add(letter)

        buf = ctypes.create_unicode_buffer(260)
        if kernel32.GetLogicalDriveStringsW(260, buf):
            for z in buf.value.split("\x00"):
                z = z.strip().rstrip("\\")
                if len(z) == 2 and z[1] == ":":
                    add_drive(z[0].upper())
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            add_drive(letter)
        return parts
    except Exception:
        return []


def _get_disks(max_parts=4):
    """ДИСКИ: C: 10% | D: 10% | ... (max_parts — сколько показывать, None = все)."""
    parts = []
    try:
        if sys.platform == "win32":
            # На Windows сначала список всех дисков из API — psutil часто отдаёт только C:
            parts = _get_disks_win32_ctypes(max_parts=None)
        if not parts and psutil:
            for p in psutil.disk_partitions(all=(sys.platform == "win32")):
                try:
                    u = psutil.disk_usage(p.mountpoint)
                    label = _disk_label(p.mountpoint)
                    if label and label != "?":
                        parts.append(f"{label}: {u.percent:.0f}%")
                except Exception:
                    continue
        if not parts and sys.platform == "win32":
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                if max_parts is not None and len(parts) >= max_parts:
                    break
                try:
                    u = psutil.disk_usage(f"{letter}:\\") if psutil else None
                    if u is not None:
                        parts.append(f"{letter}: {u.percent:.0f}%")
                except Exception:
                    continue
        if not parts and sys.platform == "win32":
            parts = _get_disks_win32_ctypes(max_parts=None)
        if max_parts is not None:
            parts = parts[:max_parts]
        return " | ".join(parts) if parts else "—"
    except Exception:
        return "—"


def _get_network_speed():
    """СЕТЬ: ↓ X кб\\с --- ↑ Y кб\\с (в КБ/с)"""
    global _net_last
    if not psutil:
        return "—"
    try:
        c = psutil.net_io_counters()
        now = time.time()
        t0, r0, s0 = _net_last
        _net_last = (now, c.bytes_recv, c.bytes_sent)
        if t0 > 0 and (now - t0) >= 0.3:
            dt = now - t0
            down_kb = (c.bytes_recv - r0) / dt / 1024
            up_kb = (c.bytes_sent - s0) / dt / 1024
            return f"{t('network_download')} ↓ {down_kb:.2f} {t('network_kbps')} --- {t('network_upload')} ↑ {up_kb:.2f} {t('network_kbps')}"
        return f"{t('network_download')} ↓ — {t('network_kbps')} --- {t('network_upload')} ↑ — {t('network_kbps')}"
    except Exception:
        return "—"


def _get_gpu():
    """ГПУ: 10% | 16128/32000 | 42° (с кэшем 3 с)."""
    key = "gpu"
    now = time.time()
    with _heavy_cache_lock:
        if key in _heavy_cache:
            ts, val = _heavy_cache[key]
            if now - ts < _HEAVY_CACHE_TTL:
                return val
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1,
            creationflags=SUBPROCESS_FLAGS,
        )
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().split("\n")[0]
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 4:
                val = f"{parts[0]}% | {parts[1]} / {parts[2]} | {parts[3]}°"
            elif len(parts) >= 3:
                val = f"{parts[0]}% | {parts[1]} / {parts[2]} | —"
            else:
                val = f"{parts[0]}% | — | —"
            with _heavy_cache_lock:
                _heavy_cache[key] = (now, val)
            return val
    except Exception:
        pass
    with _heavy_cache_lock:
        _heavy_cache[key] = (now, "—")
    return "—"


def _get_battery():
    """Батарея: % заряда, питание от сети, время до разряда (только в окне)."""
    if not psutil:
        return "—"
    try:
        bat = getattr(psutil, "sensors_battery", None)
        if not bat:
            return "—"
        b = bat()
        if b is None:
            return "—"
        pct = getattr(b, "percent", None)
        plugged = getattr(b, "power_plugged", None)
        secs = getattr(b, "secsleft", None)
        parts = []
        if pct is not None:
            parts.append(f"{pct}%")
        if plugged is not None:
            parts.append(t("battery_plugged") if plugged else t("battery_unplugged"))
        if secs is not None and not plugged and secs != -1:
            if secs == float("inf"):
                parts.append("∞ до разряда")
            else:
                m = int(secs // 60)
                parts.append(f"~{m} мин до разряда")
        return " | ".join(parts) if parts else "—"
    except Exception:
        return "—"


def _get_uptime():
    """Время работы ПК (uptime): часы/дни."""
    if not psutil:
        return "—"
    try:
        t = psutil.boot_time()
        secs = time.time() - t
        if secs < 0:
            return "—"
        days = int(secs // 86400)
        hours = int((secs % 86400) // 3600)
        mins = int((secs % 3600) // 60)
        if days > 0:
            return f"{days} д {hours} ч"
        if hours > 0:
            return f"{hours} ч {mins} мин"
        return f"{mins} мин"
    except Exception:
        return "—"


def _get_swap():
    """Файл подкачки (swap): занято/всего МБ."""
    if not psutil:
        return "—"
    try:
        s = psutil.swap_memory()
        used_mb = s.used // (1024 * 1024)
        total_mb = s.total // (1024 * 1024)
        return f"{used_mb} / {total_mb} МБ"
    except Exception:
        return "—"


def _get_process_count():
    """Число запущенных процессов."""
    if not psutil:
        return "—"
    try:
        return str(len(psutil.pids()))
    except Exception:
        return "—"


def _get_ping():
    """Пинг до 8.8.8.8 или онлайн/офлайн."""
    try:
        r = subprocess.run(
            ["ping", "-n", "1", "8.8.8.8"] if sys.platform == "win32" else ["ping", "-c", "1", "8.8.8.8"],
            capture_output=True, text=True, timeout=3,
            creationflags=SUBPROCESS_FLAGS,
        )
        if r.returncode != 0:
            return t("ping_offline")
        # "Время = 12 мс" (Windows) или "time=12.3 ms" (Linux)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"=\s*([\d.]+)\s*м?с", out, re.I) or re.search(r"time[=:\s]+([\d.]+)", out, re.I)
        if m:
            return f"{float(m.group(1)):.0f} мс"
        return t("ping_online")
    except Exception:
        return t("ping_offline")


def _get_net_total():
    """Суммарный трафик: ↓ X ГБ ↑ Y ГБ (из net_io_counters)."""
    if not psutil:
        return "—"
    try:
        c = psutil.net_io_counters()
        down_gb = c.bytes_recv / (1024 ** 3)
        up_gb = c.bytes_sent / (1024 ** 3)
        return f"{t('network_download')} ↓ {down_gb:.2f} {t('network_gb')} --- {t('network_upload')} ↑ {up_gb:.2f} {t('network_gb')}"
    except Exception:
        return "—"


def _collect_all(max_cores=64):
    """Один полный сбор всех показателей. Возвращает dict строк для кэша."""
    cpu = _get_cpu(interval=0)
    cpu_temp = _get_cpu_temp()
    cpu_line = f"{t('visible_cpu')}: {cpu} | {cpu_temp}°" if cpu_temp else f"{t('visible_cpu')}: {cpu} | —"
    return {
        "cpu_line": cpu_line,
        "cpu_temp_note": not cpu_temp and sys.platform == "win32",
        "cores": _get_cores(max_cores=max_cores),
        "gpu": _get_gpu(),
        "ram": _get_ram(),
        "disks": _get_disks(max_parts=None),
        "network_speed": _get_network_speed(),
        "battery": _get_battery(),
        "uptime": _get_uptime(),
        "swap": _get_swap(),
        "process_count": _get_process_count(),
        "ping": _get_ping(),
        "net_total": _get_net_total(),
    }


def refresh_stats_cache():
    """Обновить общий кэш статистики (подсказка/оверлей — 8 ядер). Если запись включена — сразу дописать в файл."""
    data = _collect_all(max_cores=8)
    with STATS_CACHE_LOCK:
        STATS_CACHE.clear()
        STATS_CACHE.update(data)
    if STATS_RECORDING:
        _append_stats_to_file()


def refresh_stats_and_tooltip(icon=None):
    """Обновить кэш и сразу обновить подсказку в трее (для ручного «Обновить»)."""
    global TRAY_LAST_TOOLTIP
    refresh_stats_cache()
    TRAY_LAST_TOOLTIP = get_tooltip_text()
    if icon is not None:
        post = getattr(icon, "_post_tip_update", None)
        if post:
            post()


def get_full_stats_lines(max_cores=64, visible=None):
    """Список строк для окна (читает из кэша). Используется для отрисовки с разделителями во всю ширину."""
    if visible is None:
        visible = {"cpu": True, "cores": True, "gpu": True, "ram": True, "disks": True, "network": True}
    try:
        with STATS_CACHE_LOCK:
            cache = dict(STATS_CACHE)
        if not cache:
            refresh_stats_cache()
            with STATS_CACHE_LOCK:
                cache = dict(STATS_CACHE)
        lines = []
        if visible.get("cpu", True) and "cpu_line" in cache:
            lines.append(cache["cpu_line"])
            if cache.get("cpu_temp_note"):
                lines.append(t("cpu_temp_note"))
        if visible.get("cores", True) and "cores" in cache:
            cores_text = _get_cores_full_cached() if (max_cores and max_cores > 8) else cache["cores"]
            lines.append(f"{t('section_cores')}: {cores_text}")
        if visible.get("gpu", True) and "gpu" in cache:
            lines.append(f"{t('section_gpu')}: {cache['gpu']}")
        if visible.get("ram", True) and "ram" in cache:
            lines.append(f"{t('section_ram')}: {cache['ram']}")
        if visible.get("disks", True) and "disks" in cache:
            lines.append(f"{t('section_disks')}: {cache['disks']}")
        # Только в окне: батарея, uptime, swap, процессы
        if "battery" in cache:
            lines.append(f"{t('section_battery')}: {cache['battery']}")
        if "uptime" in cache:
            lines.append(f"{t('section_uptime')}: {cache['uptime']}")
        if "swap" in cache:
            lines.append(f"{t('section_swap')}: {cache['swap']}")
        if "process_count" in cache:
            lines.append(f"{t('section_processes')}: {cache['process_count']}")
        # Сеть в конец: скорость (скачивание/отдача), пинг, суммарный трафик
        if visible.get("network", True) and "network_speed" in cache:
            lines.append(f"{t('section_network')}: {cache['network_speed']}")
        if "ping" in cache:
            lines.append(f"{t('section_ping')}: {cache['ping']}")
        if "net_total" in cache:
            lines.append(f"{t('section_net_total')}: {cache['net_total']}")
        return lines if lines else []
    except Exception:
        return []


def get_full_stats_text(max_cores=64, visible=None):
    """Полный текст для окна (одной строкой с разделителями); для вывода в одно поле."""
    lines = get_full_stats_lines(max_cores=max_cores, visible=visible)
    if not lines:
        return t("placeholder_no_sections")
    return "\n—\n".join(lines)


def get_tooltip_text(max_length=TOOLTIP_MAX):
    """Подсказка трея (или полный текст для оверлея). max_length=TOOLTIP_MAX для трея (лимит Windows 128), None — без обрезки."""
    try:
        with STATS_CACHE_LOCK:
            cache = dict(STATS_CACHE)
        lines = []
        if TRAY_VISIBLE.get("cpu", True) and "cpu_line" in cache:
            lines.append(cache["cpu_line"])
        if TRAY_VISIBLE.get("cores", True) and "cores" in cache:
            lines.append(f"{t('section_cores')}: {cache['cores']}")
        if TRAY_VISIBLE.get("gpu", True) and "gpu" in cache:
            lines.append(f"{t('section_gpu')}: {cache['gpu']}")
        if TRAY_VISIBLE.get("ram", True) and "ram" in cache:
            lines.append(f"{t('section_ram')}: {cache['ram']}")
        if TRAY_VISIBLE.get("disks", True) and "disks" in cache:
            lines.append(f"{t('section_disks')}: {cache['disks']}")
        if TRAY_VISIBLE.get("network", True) and "network_speed" in cache:
            net = cache["network_speed"].replace(t("network_download") + " ", "").replace(" " + t("network_upload") + " ", " ")
            lines.append(f"{t('section_network')}: {net}")
        text = "\n".join(lines) if lines else t("tooltip_enable_lines")
        if max_length is not None and len(text) > max_length:
            text = text[: max_length - 1] + "…"
        return text
    except Exception:
        return t("fallback_cpu_ram")


def _icon_path():
    """Путь к tray_stats.ico: рядом с exe/run.py или в распакованном bundle (exe)."""
    base = _base_path()
    path_next_to_exe = os.path.join(base, TRAY_ICON_FILENAME)
    if os.path.isfile(path_next_to_exe):
        return os.path.abspath(path_next_to_exe)
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        path_in_bundle = os.path.join(sys._MEIPASS, TRAY_ICON_FILENAME)
        if os.path.isfile(path_in_bundle):
            return os.path.abspath(path_in_bundle)
    return os.path.abspath(path_next_to_exe)


def _load_icon_image():
    """Загрузить иконку из tray_stats.ico. Если файла нет — минимальная заглушка 64x64."""
    path = _icon_path()
    try:
        if path and os.path.isfile(path):
            img = Image.open(path).convert("RGBA")
            # Для трея на Windows удобнее размер 64–256 px; 256 подходит и для окна
            if img.size != (256, 256):
                img = img.resize((256, 256), Image.Resampling.LANCZOS)
            return img
    except Exception:
        pass
    # Заглушка: маленький непрозрачный квадрат, чтобы приложение не падало
    return Image.new("RGBA", (64, 64), (60, 80, 140, 255))


def _run_overlay_window():
    """Окно-оверлей: подложка отдельно (с альфой), текст поверх без прозрачности. Прозрачная = без подложки."""
    global _overlay_root
    if sys.platform != "win32":
        return
    try:
        import tkinter as tk
        from tkinter import font as tkfont
        import ctypes
    except ImportError:
        return
    TRANSPARENT_KEY_COLOR = "#010101"
    bg_config = {
        "transparent": ("transparent", None),
        "black_25": ("#000000", 0.25),
        "black_50": ("#000000", 0.5),
        "white_25": ("#ffffff", 0.25),
        "white_50": ("#ffffff", 0.5),
    }
    GWL_EXSTYLE = -20
    WS_EX_LAYERED = 0x80000
    WS_EX_TRANSPARENT = 0x20
    WS_EX_TOPMOST = 0x8
    LWA_COLORKEY = 1
    LWA_ALPHA = 2
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x2, 0x1, 0x8
    SWP_FRAMECHANGED = 0x20
    HWND_TOP = 0
    HWND_TOPMOST = ctypes.c_void_p(-1)  # окно поверх обычных (в т.ч. поверх полноэкранных в borderless)

    # Корневое окно = подложка (нижний слой): только цвет + альфа
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(1)
    root.attributes("-topmost", True)
    root.configure(bg=TRANSPARENT_KEY_COLOR)
    underlay_frame = tk.Frame(root, bg=TRANSPARENT_KEY_COLOR, padx=8, pady=6)
    underlay_frame.pack()
    root.minsize(120, 60)

    # Окно текста (верхний слой): Toplevel того же root — один mainloop, текст с color key
    text_win = tk.Toplevel(root)
    text_win.withdraw()
    text_win.overrideredirect(1)
    text_win.attributes("-topmost", True)
    text_win.configure(bg=TRANSPARENT_KEY_COLOR)
    lab = tk.Label(
        text_win, text="…", font=("Consolas", 10, "normal"), fg=OVERLAY_TEXT_COLOR, bg=TRANSPARENT_KEY_COLOR,
        justify=tk.LEFT, padx=8, pady=6, highlightthickness=0,
    )
    lab.pack()
    text_win.minsize(120, 60)

    _click_through_done_hwnds = set()  # клик-сквозь выставляем один раз на окно — без повтора (нагрузка и мерцание)

    def set_click_through(hwnd):
        """Окно прозрачно для мыши и поверх остальных (topmost). Вызывать один раз на окно после показа."""
        if not hwnd or hwnd in _click_through_done_hwnds:
            return
        try:
            u32 = ctypes.windll.user32
            ex_style = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ex_style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
            # HWND_TOPMOST — окно остаётся поверх обычных (в т.ч. borderless fullscreen)
            u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
            _click_through_done_hwnds.add(hwnd)
        except Exception:
            pass

    GA_ROOT = 2

    def get_toplevel_hwnd(widget):
        """Реальный HWND топлевела (для Toplevel/корня окна)."""
        try:
            h = widget.winfo_id()
            u32 = ctypes.windll.user32
            root_h = u32.GetAncestor(h, GA_ROOT)
            return root_h if root_h else h
        except Exception:
            return widget.winfo_id()

    def set_layered_style():
        key = OVERLAY_BACKGROUND
        cfg = bg_config.get(key, ("transparent", None))
        root.update_idletasks()
        text_win.update_idletasks()
        h_under = get_toplevel_hwnd(root)
        h_text = get_toplevel_hwnd(text_win)
        set_click_through(h_under)
        set_click_through(h_text)
        cr_key = 0x010101
        ctypes.windll.user32.SetLayeredWindowAttributes(h_text, cr_key, 255, LWA_COLORKEY)
        if cfg[0] == "transparent" or cfg[1] is None:
            root.withdraw()
        else:
            alpha_byte = int(cfg[1] * 255)
            ctypes.windll.user32.SetLayeredWindowAttributes(h_under, 0, alpha_byte, LWA_ALPHA)

    def apply_overlay_style():
        key = OVERLAY_BACKGROUND
        cfg = bg_config.get(key, ("transparent", None))
        if cfg[0] == "transparent" or cfg[1] is None:
            root.configure(bg=TRANSPARENT_KEY_COLOR)
            underlay_frame.configure(bg=TRANSPARENT_KEY_COLOR)
        else:
            root.configure(bg=cfg[0])
            underlay_frame.configure(bg=cfg[0])
        lab.configure(fg=OVERLAY_TEXT_COLOR, bg=TRANSPARENT_KEY_COLOR)
        text_win.configure(bg=TRANSPARENT_KEY_COLOR)
        # Шрифт кортежем (family, size, weight) — так жирность гарантированно применяется на Windows
        lab.configure(font=("Consolas", 10, "bold" if OVERLAY_BOLD else "normal"))
        text_win.update_idletasks()
        set_layered_style()

    def place_at_corner():
        text_win.update_idletasks()
        w = text_win.winfo_reqwidth()
        h = text_win.winfo_reqheight()
        sw = text_win.winfo_screenwidth()
        sh = text_win.winfo_screenheight()
        pad = 8
        pos = OVERLAY_POSITION
        if pos == "top_left":
            x, y = pad, pad
        elif pos == "top_right":
            x, y = sw - w - pad, pad
        elif pos == "bottom_left":
            x, y = pad, sh - h - pad
        else:
            x, y = sw - w - pad, sh - h - pad
        root.geometry(f"{w}x{h}+{x}+{y}")
        text_win.geometry(f"+{x}+{y}")

    def shutdown_check():
        if not OVERLAY_ENABLED or not text_win.winfo_exists():
            try:
                root.withdraw()
                root.quit()
            except Exception:
                pass
            return
        if text_win.winfo_exists():
            root.after(200, shutdown_check)

    def tick():
        global OVERLAY_ENABLED
        if not OVERLAY_ENABLED or not text_win.winfo_exists():
            try:
                root.withdraw()
                root.quit()
            except Exception:
                pass
            return
        try:
            apply_overlay_style()
            text = get_tooltip_text(max_length=None)
            if not (text or "").strip():
                text = t("fallback_cpu_ram")
            lab.config(text=text)
            text_win.update_idletasks()
            place_at_corner()
            if bg_config.get(OVERLAY_BACKGROUND, ("transparent", None))[1] is not None:
                root.deiconify()
            root.update_idletasks()
            text_win.deiconify()
            text_win.update_idletasks()
            set_layered_style()
        except Exception:
            pass
        if text_win.winfo_exists():
            root.after(1500, tick)

    _overlay_root = root

    try:
        refresh_stats_cache()
    except Exception:
        pass
    apply_overlay_style()
    root.after(100, tick)
    root.after(100, shutdown_check)
    root.mainloop()
    _overlay_root = None


def _wait_overlay_stop(timeout=0.35):
    """Снять флаг оверлея и кратко подождать завершения потока (shutdown_check каждые 200 мс)."""
    global OVERLAY_ENABLED
    OVERLAY_ENABLED = False
    if _overlay_thread is None:
        return
    deadline = time.time() + timeout
    while _overlay_thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)


def _start_overlay_if_enabled():
    """Запустить оверлей в отдельном потоке, если OVERLAY_ENABLED."""
    global _overlay_thread, OVERLAY_ENABLED
    if not OVERLAY_ENABLED:
        return
    if _overlay_thread is not None and _overlay_thread.is_alive():
        return
    _overlay_thread = threading.Thread(target=_run_overlay_window, daemon=True)
    _overlay_thread.start()


# На Windows обновление подсказки должно выполняться в потоке трея — используем своё сообщение
WM_UPDATE_TIP = 0x400 + 20  # WM_USER + 20


def _update_tooltip(icon):
    """Обновляем общий кэш по TRAY_INTERVAL, подсказку из кэша (запись в файл — внутри refresh_stats_cache, если включена)."""
    global TRAY_LAST_TOOLTIP
    time.sleep(1)  # дать иконке и _hwnd появиться
    try:
        post_message = getattr(icon, "_post_tip_update", None)
    except Exception:
        post_message = None
    last_refresh = 0.0
    while getattr(icon, "visible", True):
        try:
            now = time.time()
            interval = TRAY_INTERVAL  # при смене настройки учтётся на следующем просмотре (через ≤ 1 с)
            if now - last_refresh >= interval:
                refresh_stats_cache()  # внутри при STATS_RECORDING уже пишет в файл
                last_refresh = now
                TRAY_LAST_TOOLTIP = get_tooltip_text()
                if post_message:
                    post_message()
                else:
                    icon.title = TRAY_LAST_TOOLTIP
        except Exception:
            pass
        sleep_sec = max(1, min(5, interval // 2))  # реже просыпаться при большом интервале
        time.sleep(sleep_sec)


def _run_stats_window(parent_root=None, icon=None):
    """Окно показателей. parent_root — если передан, Toplevel; иначе свой Tk(). icon — для кнопки «Выключить» (полный выход из приложения)."""
    global _stats_window_root, _stats_window_close_requested
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except ImportError:
        return

    global TRAY_VISIBLE, TRAY_INTERVAL

    # Переменные tk при сборке мусора после закрытия окна дергают Tk из другого потока — оборачиваем __del__
    class _SafeIntVar(tk.IntVar):
        def __del__(self):
            try:
                super().__del__()
            except Exception:
                pass

    class _SafeBooleanVar(tk.BooleanVar):
        def __del__(self):
            try:
                super().__del__()
            except Exception:
                pass

    # Палитры тем (Win11 светлая / тёмная)
    THEME_PALETTES = {
        "light": {
            "BG": "#f3f3f3", "CARD": "#ffffff", "TEXT": "#202020", "ACCENT": "#0078d4",
            "SUBTLE": "#605e5c", "RADIO_BG": "#e5e5e5", "BORDER": "#e5e5e5",
            "BTN_HOVER": "#106ebe", "DANGER": "#c42b1c", "DANGER_HOVER": "#a32618",
            "SEP_COLOR": "#e5e5e5",
        },
        "dark": {
            "BG": "#202020", "CARD": "#2d2d2d", "TEXT": "#e5e5e5", "ACCENT": "#60c6ff",
            "SUBTLE": "#a0a0a0", "RADIO_BG": "#3d3d3d", "BORDER": "#404040",
            "BTN_HOVER": "#7ec8ff", "DANGER": "#e05545", "DANGER_HOVER": "#c44",
            "SEP_COLOR": "#404040",
        },
    }
    cfg = load_settings()
    theme_setting = cfg.get("theme", "system")
    effective_theme = _get_system_theme() if theme_setting == "system" else theme_setting
    palette = THEME_PALETTES.get(effective_theme, THEME_PALETTES["light"])
    BG = palette["BG"]
    CARD = palette["CARD"]
    TEXT = palette["TEXT"]
    ACCENT = palette["ACCENT"]
    SUBTLE = palette["SUBTLE"]
    RADIO_BG = palette["RADIO_BG"]
    BORDER = palette["BORDER"]
    BTN_HOVER = palette["BTN_HOVER"]
    DANGER = palette["DANGER"]
    DANGER_HOVER = palette["DANGER_HOVER"]
    SEP_COLOR = palette["SEP_COLOR"]

    root = tk.Toplevel(parent_root) if parent_root else tk.Tk()
    _stats_window_root = root  # глобальная ссылка для клика по иконке (закрыть окно)
    root.title(t("window_title"))
    root.resizable(True, True)
    root.minsize(420, 380)
    root.configure(bg=BG)
    if parent_root:
        root.transient(parent_root)
    if sys.platform == "win32":
        try:
            root.attributes("-toolwindow", False)
        except Exception:
            pass
    # Иконка окна — та же, что в трее (готовый tray_stats.ico)
    try:
        icon_ico_path = _icon_path()
        if os.path.isfile(icon_ico_path) and sys.platform == "win32":
            root.iconbitmap(icon_ico_path)
        else:
            from PIL import ImageTk
            _win_icon_img = _load_icon_image()
            _win_icon_small = _win_icon_img.resize((256, 256), Image.Resampling.LANCZOS)
            root._icon_photo = ImageTk.PhotoImage(_win_icon_small)
            root.iconphoto(True, root._icon_photo)
    except Exception:
        pass

    font_mono = tkfont.Font(family="Consolas", size=11)
    font_ui = tkfont.Font(family="Segoe UI", size=10)
    font_icon = tkfont.Font(family="Segoe MDL2 Assets", size=12) if sys.platform == "win32" else font_ui

    def _make_icon_btn(parent, icon_char, label_text, cmd, bg, fg="white", hover_bg=None):
        """Кнопка в стиле Win11: иконка (Segoe MDL2 Assets) + текст. Возвращает (frame, text_label)."""
        hover_bg = hover_bg or bg
        f = tk.Frame(parent, bg=bg, cursor="hand2", padx=12, pady=6)
        try:
            il = tk.Label(f, text=icon_char, font=font_icon, fg=fg, bg=bg)
            il.pack(side=tk.LEFT, padx=(0, 6))
        except Exception:
            pass
        tl = tk.Label(f, text=label_text, font=font_ui, fg=fg, bg=bg)
        tl.pack(side=tk.LEFT)
        def _bind_enter(e):
            f.configure(bg=hover_bg)
            for c in f.winfo_children():
                try:
                    c.configure(bg=hover_bg)
                except Exception:
                    pass
        def _bind_leave(e):
            f.configure(bg=bg)
            for c in f.winfo_children():
                try:
                    c.configure(bg=bg)
                except Exception:
                    pass
        def _run(e=None):
            cmd()
        f.bind("<Button-1>", _run)
        for c in f.winfo_children():
            c.bind("<Button-1>", _run)
        f.bind("<Enter>", _bind_enter)
        f.bind("<Leave>", _bind_leave)
        for c in f.winfo_children():
            c.bind("<Enter>", _bind_enter)
            c.bind("<Leave>", _bind_leave)
        return f, tl

    # Загружаем сохранённые настройки (cfg уже загружен выше для темы)
    vis = cfg.get("tray_visible", TRAY_VISIBLE)
    interval_sec = _SafeIntVar(root, value=cfg.get("tray_interval", TRAY_INTERVAL))
    visible = {
        "cpu": _SafeBooleanVar(root, value=vis.get("cpu", True)),
        "cores": _SafeBooleanVar(root, value=vis.get("cores", True)),
        "gpu": _SafeBooleanVar(root, value=vis.get("gpu", True)),
        "ram": _SafeBooleanVar(root, value=vis.get("ram", True)),
        "disks": _SafeBooleanVar(root, value=vis.get("disks", True)),
        "network": _SafeBooleanVar(root, value=vis.get("network", True)),
    }
    autostart_var = _SafeBooleanVar(root, value=cfg.get("autostart", False))
    overlay_var = _SafeBooleanVar(root, value=cfg.get("overlay_enabled", False))
    overlay_bold_var = _SafeBooleanVar(root, value=cfg.get("overlay_bold", False))
    overlay_color_var = tk.StringVar(root, value=cfg.get("overlay_text_color", OVERLAY_TEXT_COLOR))
    overlay_position_var = tk.StringVar(root, value=cfg.get("overlay_position", OVERLAY_POSITION))
    overlay_bg_var = tk.StringVar(root, value=cfg.get("overlay_background", OVERLAY_BACKGROUND))

    def apply_tray_settings():
        """Перенести настройки в глобальные и сохранить в файл."""
        global TRAY_VISIBLE, TRAY_INTERVAL, OVERLAY_ENABLED, OVERLAY_TEXT_COLOR, OVERLAY_POSITION, OVERLAY_BACKGROUND, OVERLAY_BOLD
        TRAY_VISIBLE = {k: v.get() for k, v in visible.items()}
        TRAY_INTERVAL = interval_sec.get()
        ov_en = overlay_var.get()
        ov_bold = overlay_bold_var.get()
        # В OptionMenu хранятся подписи; переводим в значения для сохранения
        ov_cl = overlay_label_to_color(overlay_color_var.get())
        ov_pos = overlay_label_to_position(overlay_position_var.get())
        ov_bg = overlay_label_to_bg(overlay_bg_var.get())
        OVERLAY_ENABLED = ov_en
        OVERLAY_TEXT_COLOR = ov_cl
        OVERLAY_POSITION = ov_pos
        OVERLAY_BACKGROUND = ov_bg
        OVERLAY_BOLD = ov_bold
        save_settings(tray_visible=TRAY_VISIBLE, tray_interval=TRAY_INTERVAL, autostart=autostart_var.get(),
                      overlay_enabled=ov_en, overlay_text_color=ov_cl, overlay_position=ov_pos, overlay_background=ov_bg,
                      overlay_bold=ov_bold)
        if sys.platform == "win32":
            set_autostart_windows(autostart_var.get())
        if ov_en:
            _start_overlay_if_enabled()

    # Блок: настройки всплывающей подсказки в трее
    tray_frame = tk.LabelFrame(root, text=t("frame_tray_caption"), font=font_ui, fg=SUBTLE, bg=BG)
    tray_frame.pack(fill=tk.X, padx=14, pady=10)

    top = tk.Frame(tray_frame, bg=BG, padx=10, pady=6)
    top.pack(fill=tk.X)
    tk.Label(top, text=t("interval_label"), font=font_ui, fg=TEXT, bg=BG).pack(side=tk.LEFT, padx=(0, 8))
    for label, sec in [(t("interval_2s"), 2), (t("interval_10s"), 10), (t("interval_30s"), 30), (t("interval_1m"), 60), (t("interval_5m"), 300), (t("interval_10m"), 600)]:
        rb = tk.Radiobutton(
            top, text=label, variable=interval_sec, value=sec, command=apply_tray_settings,
            font=font_ui, fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
            activeforeground=TEXT, highlightthickness=0,
        )
        rb.pack(side=tk.LEFT, padx=4)

    row1 = tk.Frame(tray_frame, bg=BG)
    row1.pack(fill=tk.X, padx=10, pady=(0, 6))
    tk.Label(row1, text=t("row_visible_label"), font=font_ui, fg=TEXT, bg=BG).pack(side=tk.LEFT, padx=(0, 10))
    for label, key in [(t("visible_cpu"), "cpu"), (t("visible_cores"), "cores"), (t("visible_gpu"), "gpu"), (t("visible_ram"), "ram"), (t("visible_disks"), "disks"), (t("visible_network"), "network")]:
        cb = tk.Checkbutton(
            row1, text=label, variable=visible[key], command=apply_tray_settings,
            font=font_ui, fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
            activeforeground=TEXT, highlightthickness=0,
        )
        cb.pack(side=tk.LEFT, padx=(0, 14))

    row2 = tk.Frame(tray_frame, bg=BG)
    row2.pack(fill=tk.X, padx=10, pady=(0, 6))
    tk.Checkbutton(
        row2, text=t("autostart"), variable=autostart_var, command=apply_tray_settings,
        font=font_ui, fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
        activeforeground=TEXT, highlightthickness=0,
    ).pack(side=tk.LEFT)
    tk.Checkbutton(
        row2, text=t("overlay_on_screen"), variable=overlay_var, command=apply_tray_settings,
        font=font_ui, fg=TEXT, bg=BG, selectcolor=CARD, activebackground=BG,
        activeforeground=TEXT, highlightthickness=0,
    ).pack(side=tk.LEFT, padx=(20, 0))

    def show_overlay_opts():
        if overlay_var.get():
            overlay_opts_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        else:
            overlay_opts_frame.pack_forget()

    overlay_var.trace_add("write", lambda *a: show_overlay_opts())
    overlay_opts_frame = tk.Frame(tray_frame, bg=BG)
    color_labels = [c[0] for c in _overlay_colors()]
    pos_labels = [p[0] for p in _overlay_positions()]
    bg_labels = [b[0] for b in _overlay_backgrounds()]
    # Кнопка-иконка "B" (жирный) слева от выпадающих списков
    def toggle_bold():
        overlay_bold_var.set(not overlay_bold_var.get())
        update_bold_btn()
        apply_tray_settings()
    def update_bold_btn():
        if overlay_bold_var.get():
            btn_bold.config(relief=tk.SUNKEN, bg=SUBTLE)
        else:
            btn_bold.config(relief=tk.FLAT, bg=CARD)
    btn_bold = tk.Button(overlay_opts_frame, text="B", font=("Consolas", 10, "bold"), width=2,
                         command=toggle_bold, fg=TEXT, bg=CARD, activeforeground=TEXT, activebackground=CARD,
                         relief=tk.FLAT, bd=1, highlightthickness=0, cursor="hand2")
    overlay_bold_var.trace_add("write", lambda *a: update_bold_btn())
    btn_bold.pack(side=tk.LEFT, padx=(0, 10))
    update_bold_btn()  # начальное состояние по настройкам
    tk.Label(overlay_opts_frame, text=t("overlay_color_label"), font=font_ui, fg=SUBTLE, bg=BG).pack(side=tk.LEFT, padx=(0, 4))
    om_color = tk.OptionMenu(overlay_opts_frame, overlay_color_var, *color_labels, command=lambda _: apply_tray_settings())
    om_color.config(font=font_ui, fg=TEXT, bg=CARD, activebackground=CARD, highlightthickness=0)
    om_color.pack(side=tk.LEFT, padx=(0, 12))
    tk.Label(overlay_opts_frame, text=t("overlay_position_label"), font=font_ui, fg=SUBTLE, bg=BG).pack(side=tk.LEFT, padx=(0, 4))
    om_pos = tk.OptionMenu(overlay_opts_frame, overlay_position_var, *pos_labels, command=lambda _: apply_tray_settings())
    om_pos.config(font=font_ui, fg=TEXT, bg=CARD, activebackground=CARD, highlightthickness=0)
    om_pos.pack(side=tk.LEFT, padx=(0, 12))
    tk.Label(overlay_opts_frame, text=t("overlay_background_label"), font=font_ui, fg=SUBTLE, bg=BG).pack(side=tk.LEFT, padx=(0, 4))
    om_bg = tk.OptionMenu(overlay_opts_frame, overlay_bg_var, *bg_labels, command=lambda _: apply_tray_settings())
    om_bg.config(font=font_ui, fg=TEXT, bg=CARD, activebackground=CARD, highlightthickness=0)
    om_bg.pack(side=tk.LEFT)
    if overlay_var.get():
        overlay_opts_frame.pack(fill=tk.X, padx=10, pady=(0, 6))

    # В var храним подписи (для OptionMenu); при apply переводим в ключи/hex
    def overlay_label_to_color(lbl):
        for label, hex in _overlay_colors():
            if label == lbl:
                return hex
        return OVERLAY_TEXT_COLOR
    def overlay_label_to_position(lbl):
        for label, key in _overlay_positions():
            if label == lbl:
                return key
        return "top_left"
    def overlay_label_to_bg(lbl):
        for label, key in _overlay_backgrounds():
            if label == lbl:
                return key
        return "transparent"
    def overlay_color_to_label(hex_val):
        for label, hex in _overlay_colors():
            if hex == hex_val:
                return label
        return color_labels[0]
    def overlay_position_to_label(key):
        for label, k in _overlay_positions():
            if k == key:
                return label
        return pos_labels[0]
    def overlay_bg_to_label(key):
        for label, k in _overlay_backgrounds():
            if k == key:
                return label
        return bg_labels[0]

    # Инициализация var подписями по сохранённым ключам
    overlay_color_var.set(overlay_color_to_label(cfg.get("overlay_text_color", "#ffffff")))
    overlay_position_var.set(overlay_position_to_label(cfg.get("overlay_position", "top_left")))
    overlay_bg_var.set(overlay_bg_to_label(cfg.get("overlay_background", "transparent")))

    # Блок с полным текстом показателей
    card = tk.Frame(root, bg=CARD, padx=2, pady=2)
    card.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))
    stats_inner = tk.Frame(card, bg=CARD)
    stats_inner.pack(fill=tk.BOTH, expand=True, padx=14, pady=12, anchor=tk.NW)
    SEP_COLOR = "#e5e5e5"  # разделитель строк (под стиль Win11)

    MAX_STATS_LINES = 25  # максимум строк в блоке показателей (виджеты создаём один раз)
    line_widgets = []  # список (label, sep_frame) для каждой строки
    for _ in range(MAX_STATS_LINES):
        lab = tk.Label(stats_inner, text="", font=font_mono, justify=tk.LEFT, fg=TEXT, bg=CARD, anchor=tk.NW)
        sep = tk.Frame(stats_inner, height=1, bg=SEP_COLOR)
        line_widgets.append((lab, sep))
    placeholder_label = tk.Label(stats_inner, text=t("placeholder_no_sections"), font=font_mono, fg=TEXT, bg=CARD, anchor=tk.NW)

    WINDOW_REFRESH_SEC = 2

    def refresh_window():
        try:
            if not root.winfo_exists():
                return
            lines = get_full_stats_lines(max_cores=64, visible=None)
            if not lines:
                placeholder_label.pack(anchor=tk.W)
                for lab, sep in line_widgets:
                    lab.pack_forget()
                    sep.pack_forget()
            else:
                placeholder_label.pack_forget()
                for i, (lab, sep) in enumerate(line_widgets):
                    if i < len(lines):
                        lab.config(text=lines[i])
                        lab.pack(anchor=tk.W, fill=tk.X)
                        if i < len(lines) - 1:
                            sep.pack(fill=tk.X, pady=2)
                        else:
                            sep.pack_forget()
                    else:
                        lab.pack_forget()
                        sep.pack_forget()
            root.after(WINDOW_REFRESH_SEC * 1000, refresh_window)
        except Exception:
            pass

    def on_refresh_click():
        """Обновить кэш и подсказку трея, затем отображение окна."""
        def _do():
            refresh_stats_and_tooltip(icon)
            try:
                root.after(0, refresh_window)
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def on_close():
        global _stats_window_root
        _stats_window_root = None
        apply_tray_settings()
        root.destroy()

    def on_exit_app_click():
        """Полностью выключить приложение (трей + оверлей + выход)."""
        _wait_overlay_stop(timeout=0.35)
        try:
            apply_tray_settings()
            root.destroy()
            if icon is not None and getattr(icon, "_tk_root", None) is not None:
                icon._tk_root.after(0, lambda: icon._tk_root.quit())
                icon.stop()
        except Exception:
            pass

    def on_toggle_recording():
        """Включить/выключить непрерывную запись статистики в файл (раз в RECORDING_INTERVAL сек)."""
        global STATS_RECORDING
        STATS_RECORDING = not STATS_RECORDING
        if rec_btn_text is not None:
            rec_btn_text.config(text=t("btn_stop_recording") if STATS_RECORDING else t("btn_start_recording"))

    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(fill=tk.X, padx=14, pady=(0, 12))
    # Иконки Segoe MDL2 Assets: Refresh E72C, Stop E71A, Power E7E8, ChromeClose E8BB
    _b, _ = _make_icon_btn(btn_frame, "\uE72C", t("btn_refresh"), on_refresh_click, ACCENT, "white", BTN_HOVER)
    _b.pack(side=tk.LEFT, padx=(0, 8))
    rec_btn_frame, rec_btn_text = _make_icon_btn(
        btn_frame, "\uE71A",
        t("btn_stop_recording") if STATS_RECORDING else t("btn_start_recording"),
        on_toggle_recording, CARD, TEXT, RADIO_BG,
    )
    rec_btn_frame.pack(side=tk.LEFT, padx=(0, 8))
    _b, _ = _make_icon_btn(btn_frame, "\uE7E8", t("btn_exit"), on_exit_app_click, DANGER, "white", DANGER_HOVER)
    _b.pack(side=tk.LEFT, padx=(0, 8))
    _b, _ = _make_icon_btn(btn_frame, "\uE8BB", t("btn_close"), on_close, CARD, TEXT, RADIO_BG)
    _b.pack(side=tk.LEFT)

    # Подпись внизу окна: слева — приложение, справа — выбор языка и темы
    footer_frame = tk.Frame(root, bg=BG)
    footer_frame.pack(side=tk.BOTTOM, fill=tk.X)
    tk.Label(
        footer_frame, text=f"{APP_NAME} | {APP_AUTHOR} | {APP_VERSION} | {APP_YEAR}",
        font=("Segoe UI", 8), fg=SUBTLE, bg=BG, anchor=tk.W,
    ).pack(side=tk.LEFT, padx=14, pady=(0, 8))

    LANG_OPTIONS = [(t("lang_rus"), "rus"), (t("lang_eng"), "eng"), (t("lang_bel"), "bel"), (t("lang_tat"), "tat"), (t("lang_chi"), "chi")]
    lang_to_label = {code: lbl for lbl, code in LANG_OPTIONS}
    label_to_lang = {lbl: code for lbl, code in LANG_OPTIONS}
    lang_var = tk.StringVar(root, value=lang_to_label.get(cfg.get("lang", "rus"), t("lang_rus")))

    def on_lang_change(*args):
        code = label_to_lang.get(lang_var.get(), "rus")
        save_settings(lang=code)
        load_lang(code)
        on_close()
        tk_main = getattr(icon, "_tk_root", None) if icon is not None else None
        if tk_main is not None:
            tk_main.after(200, lambda: _show_full_stats(icon, None))

    lang_btn_frame = tk.Frame(footer_frame, bg=BG)
    lang_btn_frame.pack(side=tk.RIGHT, padx=(14, 0), pady=(0, 6))
    om_lang = tk.OptionMenu(lang_btn_frame, lang_var, *[lbl for lbl, _ in LANG_OPTIONS], command=lambda _: on_lang_change())
    om_lang.config(font=("Segoe UI", 9), fg=TEXT, bg=CARD, activebackground=CARD, highlightthickness=0, width=12)
    om_lang.pack(side=tk.LEFT)

    THEME_LABELS = [(t("theme_light"), "light"), (t("theme_dark"), "dark"), (t("theme_system"), "system")]
    theme_to_label = {v: lbl for lbl, v in THEME_LABELS}
    label_to_theme = {lbl: v for lbl, v in THEME_LABELS}
    theme_var = tk.StringVar(root, value=theme_to_label.get(theme_setting, t("theme_system")))

    def on_theme_change(*args):
        val = label_to_theme.get(theme_var.get(), "system")
        save_settings(theme=val)
        on_close()
        tk_main = getattr(icon, "_tk_root", None) if icon is not None else None
        if tk_main is not None:
            tk_main.after(200, lambda: _show_full_stats(icon, None))

    theme_btn_frame = tk.Frame(footer_frame, bg=BG)
    theme_btn_frame.pack(side=tk.RIGHT, padx=14, pady=(0, 6))
    try:
        tk.Label(theme_btn_frame, text="\uE793", font=font_icon, fg=SUBTLE, bg=BG).pack(side=tk.LEFT, padx=(0, 4))
    except Exception:
        pass
    om_theme = tk.OptionMenu(theme_btn_frame, theme_var, *[lbl for lbl, _ in THEME_LABELS], command=lambda _: on_theme_change())
    om_theme.config(font=("Segoe UI", 9), fg=TEXT, bg=CARD, activebackground=CARD, highlightthickness=0, width=14)
    om_theme.pack(side=tk.LEFT)

    def _check_close_requested():
        global _stats_window_root, _stats_window_close_requested
        if _stats_window_close_requested and root.winfo_exists():
            _stats_window_close_requested = False
            _stats_window_root = None
            try:
                apply_tray_settings()
                root.destroy()
            except Exception:
                pass
            return
        if root.winfo_exists():
            root.after(400, _check_close_requested)

    root.after(100, refresh_window)
    root.after(400, _check_close_requested)
    root.protocol("WM_DELETE_WINDOW", on_close)
    if not parent_root:
        root.mainloop()


def _show_full_stats(icon, _):
    # Окно в отдельном потоке с своим Tk(); передаём icon для кнопки «Выключить» (выход из приложения)
    threading.Thread(target=lambda: _run_stats_window(None, icon), daemon=True).start()


def _do_tray_activate_main(icon):
    """Выполняется в главном потоке (Tk). Открыть окно показателей или поднять его (не закрывать по клику в трее)."""
    root = _stats_window_root
    try:
        if root is not None and root.winfo_exists():
            root.lift()
            root.focus_force()
            return
    except Exception:
        pass
    _show_full_stats(icon, None)


def _poll_tray_activate_queue(tk_root):
    """Главный поток: обработать запросы из очереди (клик по иконке трея)."""
    try:
        while True:
            try:
                icon = _tray_activate_queue.get_nowait()
            except queue.Empty:
                break
            _do_tray_activate_main(icon)
    except Exception:
        pass
    if tk_root is not None:
        try:
            tk_root.after(150, lambda: _poll_tray_activate_queue(tk_root))
        except Exception:
            pass


def _on_tray_activate(icon, _=None):
    """Клик по иконке: открыть окно или закрыть. Вызывается из потока трея — Tk только из главного потока, кладём запрос в очередь."""
    try:
        _tray_activate_queue.put_nowait(icon)
    except Exception:
        pass


def main():
    global TRAY_VISIBLE, TRAY_INTERVAL, OVERLAY_ENABLED, OVERLAY_TEXT_COLOR, OVERLAY_POSITION, OVERLAY_BACKGROUND, OVERLAY_BOLD
    if pystray is None:
        raise ImportError("Нет pystray. Выполните: pip install pystray")
    if psutil is None:
        raise ImportError("Нет psutil. Выполните: pip install psutil")

    # Загрузка сохранённых настроек
    cfg = load_settings()
    load_lang(cfg.get("lang", "rus"))
    default_visible = {"cpu": True, "cores": True, "gpu": True, "ram": True, "disks": True, "network": True}
    TRAY_VISIBLE = {**default_visible, **cfg.get("tray_visible", {})}
    if cfg.get("tray_interval") is not None:
        TRAY_INTERVAL = int(cfg["tray_interval"])
    if cfg.get("autostart") is True and sys.platform == "win32":
        set_autostart_windows(True)
    elif sys.platform == "win32":
        set_autostart_windows(False)
    if cfg.get("overlay_enabled") is True:
        OVERLAY_ENABLED = True
    if cfg.get("overlay_text_color"):
        OVERLAY_TEXT_COLOR = str(cfg["overlay_text_color"])
    if cfg.get("overlay_position"):
        OVERLAY_POSITION = str(cfg["overlay_position"])
    if cfg.get("overlay_background"):
        OVERLAY_BACKGROUND = str(cfg["overlay_background"])
    if cfg.get("overlay_bold") is True:
        OVERLAY_BOLD = True

    # Иконка трея — готовый tray_stats.ico (файл должен лежать рядом с exe/run.py)
    icon_image = _load_icon_image()
    menu = pystray.Menu(
        pystray.MenuItem(t("menu_show_stats"), _on_tray_activate, default=True),
        pystray.MenuItem(t("menu_exit"), lambda i, _: i.stop()),
    )
    icon = pystray.Icon("system_stats", icon_image, get_tooltip_text(), menu=menu)

    # Tk в главном потоке только для корректного выхода (Выход → quit без Tcl_AsyncDelete)
    tk_root = None
    try:
        import tkinter as tk
        tk_root = tk.Tk()
        tk_root.withdraw()
        icon._tk_root = tk_root

        def _do_quit():
            try:
                tk_root.quit()
            except Exception:
                pass

        def _on_exit(i, _):
            _wait_overlay_stop(timeout=0.35)
            try:
                tk_root.after(0, _do_quit)
            except Exception:
                pass
            i.stop()

        def _on_toggle_recording(icon_ref, on_exit):
            global STATS_RECORDING
            STATS_RECORDING = not STATS_RECORDING
            icon_ref.menu = _make_tray_menu(icon_ref, on_exit)

        def _on_toggle_overlay(icon_ref, on_exit):
            global OVERLAY_ENABLED
            OVERLAY_ENABLED = not OVERLAY_ENABLED
            save_settings(overlay_enabled=OVERLAY_ENABLED)
            if OVERLAY_ENABLED:
                _start_overlay_if_enabled()
            else:
                _wait_overlay_stop(timeout=0.35)
            icon_ref.menu = _make_tray_menu(icon_ref, on_exit)

        def _make_tray_menu(icon_ref, on_exit):
            return pystray.Menu(
                pystray.MenuItem(t("menu_show_stats"), _on_tray_activate, default=True),
                pystray.MenuItem(t("menu_overlay_off") if OVERLAY_ENABLED else t("menu_overlay_on"), lambda i, _: _on_toggle_overlay(i, on_exit)),
                pystray.MenuItem(t("menu_refresh"), lambda i, _: refresh_stats_and_tooltip(i)),
                pystray.MenuItem(t("menu_recording_off") if STATS_RECORDING else t("menu_recording_on"), lambda i, _: _on_toggle_recording(i, on_exit)),
                pystray.MenuItem(t("menu_exit"), on_exit),
            )

        icon.menu = _make_tray_menu(icon, _on_exit)
    except Exception:
        icon._tk_root = None

    # На Windows подсказка обновляется только из потока трея — вешаем свой обработчик и PostMessage
    if sys.platform == "win32" and hasattr(icon, "_message_handlers"):
        import ctypes

        def _on_update_tip(w, l):
            try:
                # Только подстановка готовой строки; get_tooltip_text() не вызывать — блокирует цикл сообщений
                if TRAY_LAST_TOOLTIP:
                    icon.title = TRAY_LAST_TOOLTIP
            except Exception:
                pass
            return 0

        def _post_tip_update():
            hwnd = getattr(icon, "_hwnd", None)
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, WM_UPDATE_TIP, 0, 0)

        icon._message_handlers[WM_UPDATE_TIP] = _on_update_tip
        icon._post_tip_update = _post_tip_update

    tooltip_thread = threading.Thread(target=_update_tooltip, args=(icon,), daemon=True)
    tooltip_thread.start()

    _start_overlay_if_enabled()

    if tk_root is not None:
        tk_root.after(150, lambda: _poll_tray_activate_queue(tk_root))
        threading.Thread(target=icon.run, daemon=True).start()
        tk_root.mainloop()
        os._exit(0)  # немедленный выход без shutdown Python — избегаем Tcl_AsyncDelete при нескольких Tk в разных потоках
    else:
        icon.run()


if __name__ == "__main__":
    main()
