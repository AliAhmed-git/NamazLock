from __future__ import annotations

import ctypes
import json
import logging
import os
import platform
import random
import sys
import threading
import time
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

import requests
import tkinter as tk
from tkinter import messagebox, ttk

# Optional packages
try:
    import colorama

    colorama.init()
except Exception:
    colorama = None

try:
    import keyboard

    _KEYBOARD_AVAILABLE = True
except Exception:
    keyboard = None
    _KEYBOARD_AVAILABLE = False

# ensure Notification/audio names exist even if import fails so static analyzers
# don't flag "possibly using variable before assignment"
Notification = None
audio = None
_WINOTIFY_AVAILABLE = False
try:
    if platform.system() == "Windows":
        from winotify import Notification, audio  # type: ignore
        _WINOTIFY_AVAILABLE = True
except Exception:
    _WINOTIFY_AVAILABLE = False
    Notification = None
    audio = None

try:
    import pystray
    from PIL import Image, ImageDraw

    _TRAY_AVAILABLE = True
except Exception:
    pystray = None
    Image = None
    ImageDraw = None
    _TRAY_AVAILABLE = False

# Logging
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "prayer_reminder.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("PrayerReminder")

# Notification helper
def notify(title: str, message: str, app_name: Optional[str] = None, timeout: int = 6, icon: Optional[str] = None) -> None:
    if _WINOTIFY_AVAILABLE:
        try:
            app_id = app_name or "Prayer Reminder"
            n = Notification(app_id=app_id, title=title or "", msg=message or "", icon=icon or None)
            try:
                n.set_audio(audio.Default, loop=False)
            except Exception:
                pass
            n.show()
            return
        except Exception:
            log.exception("winotify notification failed")
    # fallback: simple log + tkinter messagebox on separate thread (non-blocking)
    log.info("Notify: %s - %s", title, message)
    def _m():
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo(title or "Prayer Reminder", message or "")
            try:
                r.destroy()
            except Exception:
                pass
        except Exception:
            pass
    threading.Thread(target=_m, daemon=True).start()

try:
    notify("Prayer Reminder Started", "Prayer Reminder is running.", app_name="Prayer Reminder")
except Exception:
    pass

# Config
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, "config.txt")  # migrate if exists
CONFIG_LOCK = threading.Lock()

DEFAULT_CONFIG = {
    "hardlock": False,
    "use_os_lock": False,
    "lock_seconds": 300,
    "selected_country": None,
    "selected_state": None,
    "selected_city": None,
    "run_on_startup": False,
    "version": 1,
}

# Globals / State
hardlock: bool = DEFAULT_CONFIG["hardlock"]
USE_OS_LOCK: bool = DEFAULT_CONFIG["use_os_lock"]
LOCK_SECONDS: int = DEFAULT_CONFIG["lock_seconds"]
selected_country: Optional[str] = DEFAULT_CONFIG["selected_country"]
selected_state: Optional[str] = DEFAULT_CONFIG["selected_state"]
selected_city: Optional[str] = DEFAULT_CONFIG["selected_city"]
run_on_startup: bool = DEFAULT_CONFIG["run_on_startup"]

PRAYER_SCHEDULE: Dict[str, Tuple[int, int]] = {}  # name -> (hour, minute)
is_locked = False
lock_mutex = threading.Lock()
last_reset_date = date.today()
prayed_today: Dict[str, bool] = {}
OFFLINE_COUNTRIES = [
    "Pakistan",
    "United States",
    "United Kingdom",
    "India",
    "Canada",
    "Australia",
    "United Arab Emirates",
    "Saudi Arabia",
    "Germany",
    "France",
]
country_list_cache: List[str] = []
country_list_loaded = False
country_list_lock = threading.Lock()

VERSES = [
    "And whoever relies upon Allah — then He is sufficient for him. (Quran 65:3)",
    "Indeed, Allah is with those who fear Him and those who are doers of good. (Quran 16:128)",
    "O you who have believed, persevere and endure and remain stationed and fear Allah. (Quran 3:200)",
    "And seek help through patience and prayer. (Quran 2:45)",
    "So do not weaken and do not grieve, for you will be superior if you are true believers. (Quran 3:139)",
    "Verily, with hardship comes ease. (Quran 94:5-6)",
    "And whoever turns to Allah — He will guide his heart. (Quran 64:11)",
    "Allah does not burden a soul beyond what it can bear. (Quran 2:286)",
    "Your Lord has not abandoned you, nor has He disliked you. (Quran 93:3)",
    "And when My servants ask you concerning Me — indeed I am near. (Quran 2:186)",
    "And speak to people good words. (Quran 2:83)",
    "Repel evil with what is better. (Quran 41:34)",
    "And whoever forgives and makes peace — his reward is with Allah. (Quran 42:40)",
    "So be patient. Indeed, the promise of Allah is truth. (Quran 30:60)",
    "Whoever is grateful, it is for the good of his own soul. (Quran 31:12)",
    "He found you lost and guided you. (Quran 93:7)",
    "Establish prayer and give zakah. (Quran general guidance)",
    "Do good; perhaps Allah will reward you. (General inspiration)",
    "Turn your face toward the light of faith and resilience.",
    "Small consistent deeds outweigh delayed grand gestures.",
    "Patience and prayer steady the heart in hardship.",
    "Seek knowledge and humility in equal measure.",
    "Good words open locked doors of hearts.",
    "Forgiveness cleanses the spirit and frees the future.",
    "Take heart; every night dissolves into dawn.",
]

# Country/state/city helpers (kept mostly from original, with safe fallbacks)
def _load_country_state_city():
    """Returns (get_countries, get_states_of_country, get_cities_of_state)."""
    import json as _json
    from types import SimpleNamespace

    cache_file = os.path.join(BASE_DIR, "country_state_cache.json")
    CACHE_TTL = 24 * 3600
    _in_memory = {"countries": None, "states": {}, "cities": {}}

    def _read_cache():
        try:
            if not os.path.exists(cache_file):
                return {}
            mtime = os.path.getmtime(cache_file)
            if time.time() - mtime > CACHE_TTL:
                return {}
            with open(cache_file, "r", encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            return {}

    def _write_cache(data):
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                _json.dump(data, f)
        except Exception:
            pass

    def _fetch_countries_from_api():
        try:
            r = requests.get("https://restcountries.com/v3.1/all", timeout=8)
            if r.status_code != 200:
                return None
            arr = r.json()
            out = []
            for item in arr:
                name = None
                iso2 = None
                try:
                    name = item.get("name", {}).get("common") or item.get("name")
                except Exception:
                    name = item.get("name")
                iso2 = item.get("cca2") or item.get("alpha2Code") or ""
                if name:
                    out.append({"name": str(name), "iso2": str(iso2 or "")})
            out.sort(key=lambda x: x["name"].lower())
            return out
        except Exception:
            return None

    def _fetch_states_from_api(country_name):
        try:
            url = "https://countriesnow.space/api/v0.1/countries/states"
            r = requests.post(url, json={"country": country_name}, timeout=8)
            if r.status_code != 200:
                return None
            data = r.json()
            if not data.get("error") and data.get("data"):
                states = data["data"].get("states") or []
                out = []
                for s in states:
                    if isinstance(s, dict):
                        nm = s.get("name") or s.get("state") or ""
                    else:
                        nm = s or ""
                    if nm:
                        out.append({"name": nm, "state_code": ""})
                out.sort(key=lambda x: x["name"].lower())
                return out
            return None
        except Exception:
            return None

    def _fetch_cities_from_api(country_name, state_name):
        try:
            url = "https://countriesnow.space/api/v0.1/countries/state/cities"
            r = requests.post(url, json={"country": country_name, "state": state_name}, timeout=8)
            if r.status_code != 200:
                return None
            data = r.json()
            if not data.get("error") and data.get("data"):
                cities = data.get("data") or []
                out = []
                for c in cities:
                    if isinstance(c, dict):
                        nm = c.get("name") or ""
                    else:
                        nm = c or ""
                    if nm:
                        out.append({"name": nm})
                out.sort(key=lambda x: x["name"].lower())
                return out
            return None
        except Exception:
            return None

    _builtin = [{"name": "Pakistan", "iso2": "PK"}, {"name": "United States", "iso2": "US"}, {"name": "United Kingdom", "iso2": "GB"}]

    cached = _read_cache()
    if cached.get("countries"):
        _in_memory["countries"] = cached["countries"]
    else:
        got = _fetch_countries_from_api()
        if got:
            _in_memory["countries"] = got
            try:
                _write_cache({"countries": got})
            except Exception:
                pass
        else:
            _in_memory["countries"] = _builtin

    def _normalize_item(item):
        if item is None:
            return None
        if isinstance(item, str):
            return SimpleNamespace(name=item, iso2="")
        if isinstance(item, dict):
            name = item.get("name") or item.get("country") or item.get("city") or ""
            iso = item.get("iso2") or item.get("code") or item.get("country_code") or ""
            state_code = item.get("state_code") or item.get("code") or item.get("state") or ""
            return SimpleNamespace(name=name, iso2=iso, state_code=state_code)
        name = getattr(item, "name", None) or getattr(item, "country", None) or getattr(item, "city", None)
        iso = getattr(item, "iso2", None) or getattr(item, "code", None) or getattr(item, "country_code", None)
        state_code = getattr(item, "state_code", None) or getattr(item, "code", None) or getattr(item, "state", None)
        return SimpleNamespace(name=name or str(item), iso2=iso or "", state_code=state_code or "")

    def _gcs():
        res = _in_memory.get("countries") or []
        return [_normalize_item(x) for x in res]

    def _gss(iso_or_country):
        if not iso_or_country:
            return []
        key = iso_or_country.strip()
        if key in _in_memory["states"]:
            return [_normalize_item(x) for x in _in_memory["states"][key]]
        country_name = None
        for c in _in_memory.get("countries", []):
            if (c.get("iso2") or "").strip().lower() == key.strip().lower():
                country_name = c.get("name")
                break
            if c.get("name", "").strip().lower() == key.strip().lower():
                country_name = c.get("name")
                break
        if not country_name:
            country_name = iso_or_country
        got = _fetch_states_from_api(country_name)
        if got:
            _in_memory["states"][key] = got
            return [_normalize_item(x) for x in got]
        _in_memory["states"][key] = []
        return []

    def _gct(iso_or_country, state):
        if not iso_or_country or not state:
            return []
        key = f"{iso_or_country.strip()}|{state.strip()}"
        if key in _in_memory["cities"]:
            return [_normalize_item({"name": x}) for x in _in_memory["cities"][key]]
        country_name = None
        for c in _in_memory.get("countries", []):
            if (c.get("iso2") or "").strip().lower() == iso_or_country.strip().lower():
                country_name = c.get("name")
                break
            if c.get("name", "").strip().lower() == iso_or_country.strip().lower():
                country_name = c.get("name")
                break
        if not country_name:
            country_name = iso_or_country
        got = _fetch_cities_from_api(country_name, state)
        if got:
            city_names = [it.get("name") if isinstance(it, dict) else str(it) for it in got]
            _in_memory["cities"][key] = city_names
            return [_normalize_item({"name": n}) for n in city_names]
        _in_memory["cities"][key] = []
        return []

    try:
        _gcs()
    except Exception:
        def _fallback_gcs(): return [_normalize_item(x) for x in _builtin]
        def _fallback_gss(iso): return []
        def _fallback_gct(iso, s): return []
        return _fallback_gcs, _fallback_gss, _fallback_gct
    return _gcs, _gss, _gct


get_countries, get_states_of_country, get_cities_of_state = _load_country_state_city()

# Utility helpers
def _normalize_list_to_names(items: Optional[List[Any]]) -> List[str]:
    names: List[str] = []
    if not items:
        return names
    for it in items:
        if it is None:
            continue
        if isinstance(it, str):
            names.append(it)
            continue
        if isinstance(it, dict):
            name = it.get("name") or it.get("city") or it.get("country") or ""
            if name:
                names.append(name)
            continue
        if hasattr(it, "name"):
            names.append(getattr(it, "name"))
            continue
        names.append(str(it))
    # remove duplicates while preserving order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out

def schedule_if_root_alive(root: Optional[tk.Tk], delay_ms: int, fn):
    try:
        if root and getattr(root, "winfo_exists", None) and root.winfo_exists():
            root.after(delay_ms, fn)
    except Exception:
        pass

def safe_get_str(widget) -> str:
    try:
        if widget and getattr(widget, "winfo_exists", None) and widget.winfo_exists():
            try:
                return widget.get()
            except Exception:
                return ""
        return ""
    except Exception:
        return ""

# Background fetchers for country/state/city lists
def fetch_country_list_background(callback=None):
    def _worker():
        global country_list_cache, country_list_loaded
        try:
            countries = get_countries()
            vals = _normalize_list_to_names(countries)
            with country_list_lock:
                country_list_cache = vals
                country_list_loaded = True
            if callback:
                try:
                    callback(vals)
                except Exception:
                    pass
        except Exception:
            with country_list_lock:
                country_list_cache = list(OFFLINE_COUNTRIES)
                country_list_loaded = True
            if callback:
                try:
                    callback(list(OFFLINE_COUNTRIES))
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()

def fetch_states_background(country_name: str, callback):
    def _worker():
        try:
            states = fetch_state_list_worker(country_name)
            if callback:
                try:
                    callback(states)
                except Exception:
                    pass
        except Exception:
            if callback:
                try:
                    callback([])
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()

def fetch_cities_background(country_name: str, state_name: str, callback):
    def _worker():
        try:
            cities = fetch_city_list_worker(country_name, state_name)
            if callback:
                try:
                    callback(cities)
                except Exception:
                    pass
        except Exception:
            if callback:
                try:
                    callback([])
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()

def fetch_state_list_worker(country_name: str) -> List[str]:
    try:
        countries = get_countries()
        iso = None
        for c in countries:
            if isinstance(c, str):
                cname = c
            else:
                cname = getattr(c, "name", None) or str(c)
            if cname and cname.strip().lower() == (country_name or "").strip().lower():
                iso = getattr(c, "iso2", None) or getattr(c, "code", None) or ""
                break
        if not iso:
            iso = country_name
        states = get_states_of_country(iso)
        return _normalize_list_to_names(states)
    except Exception:
        return []

def fetch_city_list_worker(country_name: str, state_name: str) -> List[str]:
    try:
        countries = get_countries()
        iso = None
        for c in countries:
            if isinstance(c, str):
                cname = c
            else:
                cname = getattr(c, "name", None) or str(c)
            if cname and cname.strip().lower() == (country_name or "").strip().lower():
                iso = getattr(c, "iso2", None) or getattr(c, "code", None) or ""
                break
        if not iso:
            iso = country_name
        states = get_states_of_country(iso)
        state_code = None
        for s in states:
            if isinstance(s, str):
                sname = s
            else:
                sname = getattr(s, "name", None) or str(s)
            if sname and sname.strip().lower() == (state_name or "").strip().lower():
                state_code = getattr(s, "state_code", None) or getattr(s, "code", None) or getattr(s, "state", None) or ""
                break
        if not state_code:
            state_code = state_name
        cities = get_cities_of_state(iso, state_code)
        return _normalize_list_to_names(cities)
    except Exception:
        return []

# Autocomplete entry (kept, minor cleanup)
class AutocompleteEntry(tk.Entry):
    def __init__(self, master=None, completevalues=None, width=50, max_results: int = 7, debounce_ms: int = 200, **kwargs):
        textvar = kwargs.pop("textvariable", None)
        if textvar is None:
            self.var = tk.StringVar()
        else:
            self.var = textvar
        super().__init__(master, textvariable=self.var, **kwargs)
        self.master = master
        self.completevalues = list(dict.fromkeys(completevalues or []))
        self.width = width
        self.max_results = max_results
        self.debounce_ms = debounce_ms
        self._after_id = None
        self._popup = None
        self._listbox = None
        self._visible = False
        self.configure(exportselection=False)
        self.var.trace_add("write", self._on_var_change)
        self.bind('<Down>', self._on_down)
        self.bind('<Up>', self._on_up)
        self.bind('<Return>', self._on_return)
        self.bind('<Escape>', self._on_escape)
        self.bind('<FocusOut>', self._on_focus_out)
        self.bind('<FocusIn>', self._on_focus_in)

    def set_values(self, values: List[str]):
        self.completevalues = list(dict.fromkeys(values or []))
        self._refresh_popup_if_visible()

    def is_valid(self) -> bool:
        v = (self.var.get() or '').strip()
        return bool(v) and any(v.lower() == s.lower() for s in self.completevalues)

    def set_max_results(self, n: int):
        self.max_results = max(1, int(n))

    def _on_var_change(self, *args):
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = self.after(self.debounce_ms, self._do_search)

    def _do_search(self):
        self._after_id = None
        text = (self.var.get() or '').strip()
        if not text:
            self._hide_popup()
            return
        matches = self._find_matches(text)
        if not matches:
            self._hide_popup()
            return
        self._show_popup(matches)

    def _find_matches(self, text: str) -> List[str]:
        text_l = text.lower()
        starts = []
        contains = []
        for v in self.completevalues:
            if not v:
                continue
            vl = v.lower()
            if vl.startswith(text_l):
                starts.append(v)
            elif text_l in vl:
                contains.append(v)
        results = starts + contains
        seen = set()
        out = []
        for r in results:
            if r not in seen:
                seen.add(r)
                out.append(r)
            if len(out) >= self.max_results:
                break
        return out

    def _show_popup(self, items: List[str]):
        if not items:
            self._hide_popup()
            return
        if not self._popup:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._popup.attributes('-topmost', True)
            self._listbox = tk.Listbox(self._popup, width=self.width, height=min(len(items), self.max_results))
            self._listbox.pack(expand=True, fill='both')
            self._listbox.bind('<Button-1>', self._on_listbox_click)
            self._listbox.bind('<Motion>', self._on_listbox_motion)
            self._listbox.bind('<Return>', self._on_return)
            self._listbox.bind('<Escape>', self._on_escape)
        else:
            try:
                self._listbox.delete(0, tk.END)
            except Exception:
                pass
        for it in items:
            try:
                self._listbox.insert(tk.END, it)
            except Exception:
                pass
        self._position_popup()
        self._visible = True
        try:
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(0)
            self._listbox.activate(0)
        except Exception:
            pass

    def _position_popup(self):
        try:
            x = self.winfo_rootx()
            y = self.winfo_rooty() + self.winfo_height()
            self._popup.geometry(f'+{x}+{y}')
        except Exception:
            pass

    def _hide_popup(self):
        if self._popup:
            try:
                self._popup.destroy()
            except Exception:
                pass
        self._popup = None
        self._listbox = None
        self._visible = False

    def _refresh_popup_if_visible(self):
        if self._visible:
            text = (self.var.get() or '').strip()
            matches = self._find_matches(text) if text else []
            if matches:
                self._show_popup(matches)
            else:
                self._hide_popup()

    def _on_listbox_click(self, event):
        if not self._listbox:
            return
        idx = self._listbox.nearest(event.y)
        try:
            val = self._listbox.get(idx)
            self.var.set(val)
        except Exception:
            pass
        self._hide_popup()
        try:
            self.icursor(tk.END)
            self.focus()
        except Exception:
            pass

    def _on_listbox_motion(self, event):
        if not self._listbox:
            return
        idx = self._listbox.nearest(event.y)
        try:
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.activate(idx)
        except Exception:
            pass

    def _on_down(self, event):
        if self._popup and self._listbox:
            cur = self._listbox.curselection()
            idx = 0 if not cur else min(self._listbox.size() - 1, cur[0] + 1)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.activate(idx)
            return 'break'
        return None

    def _on_up(self, event):
        if self._popup and self._listbox:
            cur = self._listbox.curselection()
            idx = max(0, self._listbox.size() - 1) if not cur else max(0, cur[0] - 1)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.activate(idx)
            return 'break'
        return None

    def _on_return(self, event=None):
        if self._popup and self._listbox:
            cur = self._listbox.curselection()
            idx = 0 if not cur else cur[0]
            try:
                val = self._listbox.get(idx)
                self.var.set(val)
            except Exception:
                pass
            self._hide_popup()
            try:
                self.icursor(tk.END)
                self.focus()
            except Exception:
                pass
            return 'break'
        return None

    def _on_escape(self, event=None):
        if self._visible:
            self._hide_popup()
            return 'break'
        return None

    def _on_focus_out(self, event=None):
        def _maybe_hide():
            if not self.focus_get() and not (self._popup and self._popup.focus_get()):
                self._hide_popup()
        try:
            self.after(120, _maybe_hide)
        except Exception:
            pass

    def _on_focus_in(self, event=None):
        self._refresh_popup_if_visible()

# City validation
def check_city_valid(country: str, city: str) -> bool:
    if not country or not city:
        return False
    try:
        url = "https://api.aladhan.com/v1/timingsByCity"
        params = {"city": city, "country": country, "method": 2}
        r = requests.get(url, params=params, timeout=7)
        if r.status_code != 200:
            return False
        data = r.json()
        timings = data.get("data", {}).get("timings")
        if not timings:
            return False
        return ("Fajr" in timings or "fajr" in timings) and ("Dhuhr" in timings or "dhuhr" in timings)
    except Exception:
        return False

# Config load/save + legacy migration
def migrate_legacy_config():
    """If legacy config.txt exists, read it and produce a JSON config to preserve user's settings."""
    if not os.path.exists(LEGACY_CONFIG_PATH):
        return None
    try:
        with open(LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
        cfg = DEFAULT_CONFIG.copy()
        if len(lines) >= 7:
            cfg["hardlock"] = lines[0].lower() == "true"
            try:
                val = int(float(lines[1]))
                cfg["lock_seconds"] = max(60, val)
            except Exception:
                cfg["lock_seconds"] = DEFAULT_CONFIG["lock_seconds"]
            cfg["use_os_lock"] = lines[2].lower() == "true"
            cfg["selected_country"] = lines[3] or None
            cfg["selected_state"] = lines[4] or None
            cfg["selected_city"] = lines[5] or None
            cfg["run_on_startup"] = lines[6].lower() == "true"
        return cfg
    except Exception:
        log.exception("Failed to migrate legacy config")
        return None

def save_config():
    cfg = {
        "hardlock": bool(hardlock),
        "use_os_lock": bool(USE_OS_LOCK),
        "lock_seconds": int(max(60, LOCK_SECONDS)),
        "selected_country": selected_country,
        "selected_state": selected_state,
        "selected_city": selected_city,
        "run_on_startup": bool(run_on_startup),
        "version": 1,
    }
    try:
        with CONFIG_LOCK:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        log.info("Config saved to %s", CONFIG_PATH)
        try:
            add_to_startup(cfg["run_on_startup"])
        except Exception:
            pass
    except Exception:
        log.exception("Error saving config")

def load_config():
    global hardlock, LOCK_SECONDS, USE_OS_LOCK, selected_country, selected_state, selected_city, run_on_startup
    # migrate legacy if needed
    if not os.path.exists(CONFIG_PATH) and os.path.exists(LEGACY_CONFIG_PATH):
        try:
            migrated = migrate_legacy_config()
            if migrated:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(migrated, f, indent=2)
                log.info("Migrated legacy config.txt to config.json")
        except Exception:
            pass
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            hardlock = bool(cfg.get("hardlock", DEFAULT_CONFIG["hardlock"]))
            try:
                LOCK_SECONDS = max(60, int(cfg.get("lock_seconds", DEFAULT_CONFIG["lock_seconds"])))
            except Exception:
                LOCK_SECONDS = DEFAULT_CONFIG["lock_seconds"]
            USE_OS_LOCK = bool(cfg.get("use_os_lock", DEFAULT_CONFIG["use_os_lock"]))
            selected_country = cfg.get("selected_country")
            selected_state = cfg.get("selected_state")
            selected_city = cfg.get("selected_city")
            run_on_startup = bool(cfg.get("run_on_startup", DEFAULT_CONFIG["run_on_startup"]))
            log.info("Config loaded: country=%s state=%s city=%s hardlock=%s os_lock=%s locksec=%s run_on_startup=%s",
                     selected_country, selected_state, selected_city, hardlock, USE_OS_LOCK, LOCK_SECONDS, run_on_startup)
            try:
                add_to_startup(run_on_startup)
            except Exception:
                pass
        except Exception:
            log.exception("Error loading config")

def add_to_startup(enable: bool):
    global run_on_startup
    run_on_startup = bool(enable)
    if platform.system() != "Windows":
        return
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_WRITE)
        try:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            if enable:
                winreg.SetValueEx(key, "PrayerReminder", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "PrayerReminder")
                except Exception:
                    pass
        finally:
            try:
                key.Close()
            except Exception:
                pass
    except Exception:
        log.exception("Failed to set startup entry (Windows)")

# Console window minimize (Windows)
def minimize_console():
    if platform.system() == "Windows":
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)
        except Exception:
            pass

# OS lock (Windows)
def os_lock_now():
    if USE_OS_LOCK and platform.system() == "Windows":
        try:
            ctypes.windll.user32.LockWorkStation()
            log.info("Called LockWorkStation()")
        except Exception:
            log.exception("Failed calling LockWorkStation")

# Lock screen UI management
_lock_ui = {"root": None, "countdown": None, "verse_label": None, "done_button": None, "verse_job": None, "tick_job": None, "hardlock_watcher_job": None}

def _schedule_on_lock_ui(fn):
    root = _lock_ui.get("root")
    if root:
        try:
            root.after(0, fn)
        except Exception:
            try:
                fn()
            except Exception:
                log.exception("Failed to run scheduled UI fn")
    else:
        try:
            fn()
        except Exception:
            log.exception("Failed to run immediate fn")

def unlock_lock_screen():
    global is_locked
    with lock_mutex:
        if not is_locked:
            return
        is_locked = False
    log.info("Unlocking lock screen.")

    def _destroy():
        root = _lock_ui.get("root")
        if root:
            try:
                if _lock_ui.get("verse_job"):
                    try:
                        root.after_cancel(_lock_ui.get("verse_job"))
                    except Exception:
                        pass
                if _lock_ui.get("tick_job"):
                    try:
                        root.after_cancel(_lock_ui.get("tick_job"))
                    except Exception:
                        pass
                if _lock_ui.get("hardlock_watcher_job"):
                    try:
                        root.after_cancel(_lock_ui.get("hardlock_watcher_job"))
                    except Exception:
                        pass
                root.destroy()
            except Exception:
                pass
        for k in list(_lock_ui.keys()):
            _lock_ui[k] = None

    _schedule_on_lock_ui(_destroy)

def _update_hardlock_ui():
    try:
        btn = _lock_ui.get("done_button")
        root = _lock_ui.get("root")
        if not root:
            return
        if hardlock:
            if btn:
                try:
                    btn.pack_forget()
                except Exception:
                    pass
                _lock_ui["done_button"] = None
        else:
            if not _lock_ui.get("done_button"):
                def _done_cb():
                    unlock_lock_screen()
                b = tk.Button(root, text="Done", font=("Helvetica", 20), command=_done_cb)
                b.pack(pady=30)
                _lock_ui["done_button"] = b
    except Exception:
        log.exception("Error updating hardlock UI")

def _verse_tick():
    try:
        root = _lock_ui.get("root")
        if not root or not is_locked:
            return
        vl = _lock_ui.get("verse_label")
        if vl:
            vl.config(text=random.choice(VERSES))
        _lock_ui["verse_job"] = root.after(10000, _verse_tick)
    except Exception:
        log.exception("Verse tick exception")

def _tick_countdown_local(end_ts: float):
    try:
        root = _lock_ui.get("root")
        if not root or not is_locked:
            return
        cd = _lock_ui.get("countdown")
        remaining = int(max(0, end_ts - time.time()))
        if cd:
            cd.config(text=f"Unlocks in: {remaining} seconds")
        if remaining <= 0:
            unlock_lock_screen()
            return
        _lock_ui["tick_job"] = root.after(250, lambda: _tick_countdown_local(end_ts))
    except Exception:
        log.exception("Countdown tick exception")

def show_lock_screen(duration_seconds: int):
    global is_locked
    with lock_mutex:
        if is_locked:
            log.info("Lock screen already active; skipping.")
            return
        is_locked = True
    os_lock_now()
    end_ts = time.time() + duration_seconds
    root = None
    try:
        root = tk.Tk()
        _lock_ui["root"] = root
        root.title("Prayer Reminder")
        root.attributes("-topmost", True)
        try:
            root.attributes("-fullscreen", True)
        except Exception:
            pass
        root.configure(bg="#111")
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        tk.Label(root, text="Time for prayer", font=("Helvetica", 48), bg="#111", fg="#ffd166").pack(pady=60)
        tk.Label(root, text="Please pray now.", font=("Helvetica", 22), bg="#111", fg="#eee").pack(pady=20)
        countdown = tk.Label(root, text="", font=("Helvetica", 20), bg="#111", fg="#ccc")
        countdown.pack(pady=10)
        _lock_ui["countdown"] = countdown
        verse_label = tk.Label(root, text=random.choice(VERSES), font=("Helvetica", 18), bg="#111", fg="#aaa", wraplength=1000, justify="center")
        verse_label.pack(pady=30)
        _lock_ui["verse_label"] = verse_label
        if not hardlock:
            def _done_cb():
                unlock_lock_screen()
            done_btn = tk.Button(root, text="Done", font=("Helvetica", 20), command=_done_cb)
            done_btn.pack(pady=30)
            _lock_ui["done_button"] = done_btn
        else:
            _lock_ui["done_button"] = None
        _verse_tick()
        _tick_countdown_local(end_ts)

        def _hardlock_watcher():
            try:
                _update_hardlock_ui()
            except Exception:
                pass
            if _lock_ui.get("root"):
                _lock_ui["hardlock_watcher_job"] = _lock_ui["root"].after(500, _hardlock_watcher)

        _hardlock_watcher()
        log.info("Lock screen shown for %s seconds (hardlock=%s).", duration_seconds, hardlock)
        root.mainloop()
    except Exception as e:
        log.exception("Exception in lock screen UI: %s", e)
    finally:
        with lock_mutex:
            is_locked = False
        for k in list(_lock_ui.keys()):
            _lock_ui[k] = None
        if root:
            try:
                root.destroy()
            except Exception:
                pass

def lock_now():
    threading.Thread(target=show_lock_screen, args=(LOCK_SECONDS,), daemon=True).start()

# Tray icon utilities
def _make_tray_image(size=64, color1=(0, 120, 212), color2=(255, 255, 255)):
    if Image is None or ImageDraw is None:
        return None
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size - 1, size - 1), fill=color1)
    draw.ellipse((size * 0.18, size * 0.18, size * 0.82, size * 0.82), fill=color2)
    return img

_tray_icon = None
_tray_thread = None

def _tray_open_setup(icon, item):
    threading.Thread(target=show_setup, daemon=True).start()

def _tray_show_status(icon, item):
    try:
        notify(title="Prayer Reminder", message=f"Location: {selected_country or '-'} / {selected_state or '-'} / {selected_city or '-'}\nHardlock: {hardlock}\nOS Lock: {USE_OS_LOCK}", app_name="Prayer Reminder", timeout=6)
    except Exception:
        def _m():
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo("Prayer Reminder", f"Location: {selected_country or '-'} / {selected_state or '-'} / {selected_city or '-'}\nHardlock: {hardlock}\nOS Lock: {USE_OS_LOCK}")
            try:
                r.destroy()
            except Exception:
                pass
        threading.Thread(target=_m, daemon=True).start()

def _tray_quit(icon, item):
    try:
        icon.stop()
    except Exception:
        pass
    try:
        os._exit(0)
    except Exception:
        sys.exit(0)

def start_tray_icon():
    global _tray_icon, _tray_thread
    if not _TRAY_AVAILABLE:
        log.info("pystray/Pillow not available; tray icon disabled.")
        return
    if _tray_icon is not None:
        return
    icon_img = _make_tray_image()
    menu = pystray.Menu(
        pystray.MenuItem("Open Setup", _tray_open_setup),
        pystray.MenuItem("Show Status", _tray_show_status),
        pystray.MenuItem("Lock Now", lambda icon, item: lock_now()),
        pystray.MenuItem("Quit", _tray_quit),
    )
    _tray_icon = pystray.Icon("PrayerReminder", icon_img, "Prayer Reminder", menu)
    def _run():
        try:
            _tray_icon.run()
        except Exception:
            log.exception("Tray icon run failed")
    _tray_thread = threading.Thread(target=_run, daemon=True)
    _tray_thread.start()
    log.info("Tray icon started.")

def _schedule_ui_update_if_locked(fn):
    root = _lock_ui.get("root")
    if root:
        try:
            root.after(0, fn)
        except Exception:
            pass
    else:
        try:
            fn()
        except Exception:
            pass

# Keyboard hook and global hotkeys
def on_key(e):
    if getattr(e, "event_type", None) and e.event_type != "down":
        return
    name = getattr(e, "name", "").lower()
    global hardlock, USE_OS_LOCK, LOCK_SECONDS
    try:
        if name == "f10":
            lock_now()
        elif name == "tab":
            if not hardlock:
                _schedule_ui_update_if_locked(unlock_lock_screen)
        elif name == "f12":
            log.info("Exit requested via F12.")
            try:
                os._exit(0)
            except Exception:
                sys.exit(0)
        elif name == "f11":
            hardlock = not hardlock
            log.info("Hardlock toggled -> %s", hardlock)
            save_config()
            _schedule_ui_update_if_locked(_update_hardlock_ui)
        elif name == "print_screen":
            USE_OS_LOCK = not USE_OS_LOCK
            log.info("USE_OS_LOCK toggled -> %s", USE_OS_LOCK)
            save_config()
        elif name == "f9":
            LOCK_SECONDS += 60
            log.info("LOCK_SECONDS -> %s", LOCK_SECONDS)
            save_config()
        elif name == "f8":
            LOCK_SECONDS = max(60, LOCK_SECONDS - 60)
            log.info("LOCK_SECONDS -> %s", LOCK_SECONDS)
            save_config()
        elif name == "end":
            threading.Thread(target=show_setup, daemon=True).start()
    except Exception:
        log.exception("Key handler error")

if _KEYBOARD_AVAILABLE:
    try:
        keyboard.hook(on_key)
        log.info("Keyboard hook registered (requires appropriate privileges on some systems).")
    except Exception:
        log.exception("Failed to register keyboard hook")
else:
    log.warning("keyboard module not available; global hotkeys disabled.")

# Prayer times fetcher (was missing in the original script)
def fetch_prayer_times(country: Optional[str] = None, city: Optional[str] = None, method: int = 2) -> None:
    """Populate PRAYER_SCHEDULE using Aladhan API (or defaults on failure)."""
    global PRAYER_SCHEDULE
    if country is None:
        country = selected_country
    if city is None:
        city = selected_city
    # default times if anything goes wrong
    defaults = {"fajr": (6, 0), "dhuhr": (13, 0), "asr": (15, 45), "maghrib": (18, 15), "isha": (20, 0)}
    if not country or not city:
        PRAYER_SCHEDULE = defaults.copy()
        return
    try:
        url = "https://api.aladhan.com/v1/timingsByCity"
        params = {"city": city, "country": country, "method": method}
        r = requests.get(url, params=params, timeout=8)
        if r.status_code != 200:
            log.warning("Aladhan API returned status %s; using defaults", r.status_code)
            PRAYER_SCHEDULE = defaults.copy()
            return
        data = r.json()
        times = data.get("data", {}).get("timings", {})
        def parse_hm(t: str) -> Tuple[int, int]:
            if not t or ":" not in t:
                return 0, 0
            try:
                parts = t.split(":")
                if len(parts) != 2:
                    return 0, 0
                h = int(parts[0])
                m = int(parts[1])
                return h, m
            except Exception:
                return 0, 0
        PRAYER_SCHEDULE = {k.lower(): parse_hm(v) for k, v in times.items() if k.lower() in ["fajr", "dhuhr", "asr", "maghrib", "isha"]}
        # ensure we have all keys
        for k, v in defaults.items():
            if k not in PRAYER_SCHEDULE or PRAYER_SCHEDULE[k] == (0, 0):
                PRAYER_SCHEDULE[k] = v
        log.info("Prayer times fetched for %s, %s: %s", city, country, PRAYER_SCHEDULE)
    except Exception:
        log.exception("Error fetching prayer times, using defaults")
        PRAYER_SCHEDULE = defaults.copy()

# Date/time helpers
def today_prayer_times() -> Dict[str, datetime]:
    now = datetime.now()
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: Dict[str, datetime] = {}
    for name, (h, m) in PRAYER_SCHEDULE.items():
        try:
            out[name] = base.replace(hour=int(h), minute=int(m))
        except Exception:
            out[name] = base.replace(hour=0, minute=0)
    return out

def reset_if_new_day():
    global prayed_today, last_reset_date
    today = date.today()
    if today != last_reset_date:
        prayed_today = {name: False for name in PRAYER_SCHEDULE}
        last_reset_date = today

# UI: setup window
def show_setup():
    """Shows the setup window to choose country/state/city and options."""
    global selected_country, selected_state, selected_city, hardlock, LOCK_SECONDS, USE_OS_LOCK, run_on_startup, PRAYER_SCHEDULE

    root = tk.Tk()
    root.title("Prayer Reminder Setup")
    root.geometry("700x460")
    root.configure(bg="#1f1f1f")
    root.resizable(False, False)

    ttk.Style().theme_use('default')

    frame = tk.Frame(root, bg="#1f1f1f")
    frame.pack(padx=18, pady=10, fill="x")

    tk.Label(root, text="Prayer Reminder Setup", font=("Helvetica", 20, "bold"), bg="#1f1f1f", fg="#ffd166").pack(pady=12)
    tk.Label(root, text="Type to search: Country → State → City", font=("Helvetica", 12), bg="#1f1f1f", fg="#eee").pack(pady=4)

    tk.Label(frame, text="Country:", bg="#1f1f1f", fg="#ffd166").grid(row=0, column=0, sticky="w", pady=8)
    tk.Label(frame, text="State / Province:", bg="#1f1f1f", fg="#ffd166").grid(row=1, column=0, sticky="w", pady=8)
    tk.Label(frame, text="City:", bg="#1f1f1f", fg="#ffd166").grid(row=2, column=0, sticky="w", pady=8)

    with country_list_lock:
        local_offline = list(OFFLINE_COUNTRIES)

    country_entry = AutocompleteEntry(frame, completevalues=local_offline, width=60)
    country_entry.grid(row=0, column=1, sticky="ew", padx=6)
    country_entry.var.set(selected_country or "")

    state_entry = AutocompleteEntry(frame, completevalues=[], width=60)
    state_entry.grid(row=1, column=1, sticky="ew", padx=6)
    state_entry.var.set(selected_state or "")

    city_entry = AutocompleteEntry(frame, completevalues=[], width=60)
    city_entry.grid(row=2, column=1, sticky="ew", padx=6)
    city_entry.var.set(selected_city or "")

    sel_lbl = tk.Label(root, text=f"Country: {selected_country or '-'}  State: {selected_state or '-'}  City: {selected_city or '-'}", bg="#1f1f1f", fg="#eee", font=("Helvetica", 12))
    sel_lbl.pack(pady=10)

    def on_countries_loaded(vals):
        try:
            if not (root and getattr(root, "winfo_exists", None) and root.winfo_exists()):
                return
            country_entry.set_values(vals)
            with country_list_lock:
                global country_list_cache, country_list_loaded
                country_list_cache = list(vals)
                country_list_loaded = True
            if selected_country:
                try:
                    country_entry.var.set(selected_country)
                except Exception:
                    pass
        except Exception:
            pass

    fetch_country_list_background(callback=lambda v: schedule_if_root_alive(root, 10, lambda v=v: on_countries_loaded(v)))

    # update state list based on country
    def update_states_from_country_debounced(name):
        if not name:
            try:
                state_entry.set_values([])
                state_entry.var.set("")
                city_entry.set_values([])
                city_entry.var.set("")
                sel_lbl.config(text=f"Country: -  State: -  City: -")
            except Exception:
                pass
            return

        def _cb(states):
            try:
                if not (root and getattr(root, "winfo_exists", None) and root.winfo_exists()):
                    return
                state_entry.set_values(states)
                if states:
                    try:
                        state_entry.var.set(states[0])
                    except Exception:
                        pass
                else:
                    try:
                        state_entry.var.set("")
                    except Exception:
                        pass
                update_cities_from_state_debounced(name, safe_get_str(state_entry).strip())
            except Exception:
                pass

        fetch_states_background(name, lambda s: schedule_if_root_alive(root, 10, lambda s=s: _cb(s)))

    # update city list based on country+state
    def update_cities_from_state_debounced(country_name, state_name):
        if not country_name or not state_name:
            try:
                city_entry.set_values([])
                city_entry.var.set("")
                sel_lbl.config(text=f"Country: {country_name or '-'}  State: {state_name or '-'}  City: -")
            except Exception:
                pass
            return

        def _cb(cities):
            try:
                if not (root and getattr(root, "winfo_exists", None) and root.winfo_exists()):
                    return
                city_entry.set_values(cities)
                if cities:
                    try:
                        city_entry.var.set(cities[0])
                    except Exception:
                        pass
                else:
                    try:
                        city_entry.var.set("")
                    except Exception:
                        pass
                sel_lbl.config(text=f"Country: {country_name or '-'}  State: {state_name or '-'}  City: {city_entry.var.get() or '-'}")
            except Exception:
                pass

        fetch_cities_background(country_name, state_name, lambda c: schedule_if_root_alive(root, 10, lambda c=c: _cb(c)))

    country_entry.var.trace_add("write", lambda *a: schedule_if_root_alive(root, 10, lambda: update_states_from_country_debounced(safe_get_str(country_entry).strip())))
    state_entry.var.trace_add("write", lambda *a: schedule_if_root_alive(root, 10, lambda: update_cities_from_state_debounced(safe_get_str(country_entry).strip(), safe_get_str(state_entry).strip())))
    city_entry.var.trace_add("write", lambda *a: schedule_if_root_alive(root, 10, lambda: sel_lbl.config(text=f"Country: {safe_get_str(country_entry).strip() or '-'}  State: {safe_get_str(state_entry).strip() or '-'}  City: {safe_get_str(city_entry).strip() or '-'}")))

    hardlock_var = tk.BooleanVar(value=hardlock)
    locksec_var = tk.StringVar(value=str(LOCK_SECONDS))
    use_os_lock_var = tk.BooleanVar(value=USE_OS_LOCK)
    run_startup_var = tk.BooleanVar(value=run_on_startup)

    ttk.Checkbutton(root, text="Enable Hard Lock (no keyboard unlock)", variable=hardlock_var).pack(pady=4)
    locksec_frame = tk.Frame(root, bg="#1f1f1f")
    locksec_frame.pack(pady=4)
    tk.Label(locksec_frame, text="Lock Duration (seconds, min 60):", bg="#1f1f1f", fg="#eee").pack(side="left")
    tk.Entry(locksec_frame, textvariable=locksec_var, width=10).pack(side="left", padx=6)
    ttk.Button(locksec_frame, text="Set", command=lambda: None).pack(side="left")
    ttk.Checkbutton(root, text="Use OS-level Lock (if supported)", variable=use_os_lock_var).pack(pady=4)
    ttk.Checkbutton(root, text="Run on startup (Windows)", variable=run_startup_var).pack(pady=4)

    def validate_selection(country: str, state: str, city: str, city_entry_obj=None) -> bool:
        with country_list_lock:
            cl = list(country_list_cache) if country_list_cache else list(OFFLINE_COUNTRIES)
        if not any(country.strip().lower() == c.strip().lower() for c in cl):
            try:
                messagebox.showwarning("Invalid Country", "Selected country is not valid. Please choose a valid country.")
            except Exception:
                pass
            return False
        states = fetch_state_list_worker(country)
        if not any(state.strip().lower() == s.strip().lower() for s in states):
            try:
                messagebox.showwarning("Invalid State", "Selected state does not belong to the selected country. Please choose a matching state.")
            except Exception:
                pass
            return False
        cities = fetch_city_list_worker(country, state)
        if any(city.strip().lower() == c.strip().lower() for c in cities):
            return True
        ok = check_city_valid(country, city)
        if ok:
            if city_entry_obj:
                try:
                    new_vals = [city] + [c for c in cities if c.strip().lower() != city.strip().lower()]
                    city_entry_obj.set_values(new_vals)
                except Exception:
                    pass
            try:
                answer = messagebox.askyesno("City not in local list", f"Online API returned results for '{city}'.\n\nSave this city?")
            except Exception:
                answer = False
            return answer
        else:
            try:
                messagebox.showwarning("Invalid City", "Selected city does not belong to the selected state (and API lookup failed).")
            except Exception:
                pass
            return False

    def show_confirmation_window(prev_vals, c, s, ci):
        conf = tk.Toplevel(root)
        conf.title("Confirm Settings")
        conf.geometry("520x220")
        conf.transient(root)
        tk.Label(conf, text="Confirm your settings", font=("Helvetica", 16, "bold")).pack(pady=10)
        tk.Label(conf, text=f"Country: {c}\nState: {s}\nCity: {ci}", justify="left").pack(pady=6)
        times_frame = tk.Frame(conf)
        times_frame.pack(pady=6, fill="x")
        tk.Label(times_frame, text="Prayer times (local preview):").pack()
        # format prayer times
        try:
            now = datetime.now()
            base = now.replace(hour=0, minute=0, second=0, microsecond=0)
            times_preview = {}
            for name, (h, m) in PRAYER_SCHEDULE.items():
                try:
                    dt = base.replace(hour=int(h), minute=int(m))
                    times_preview[name.title()] = dt.strftime("%I:%M %p")
                except Exception:
                    times_preview[name.title()] = f"{h}:{m}"
            txt = "\n".join([f"{k}: {v}" for k, v in times_preview.items()])
            tk.Label(conf, text=txt, justify="left").pack()
        except Exception:
            pass

        btn_frame = tk.Frame(conf)
        btn_frame.pack(pady=12, fill="x")
        def on_back():
            try:
                conf.destroy()
            except Exception:
                pass

        def on_confirm():
            try:
                val = int(float(locksec_var.get()))
            except Exception:
                val = LOCK_SECONDS
            # apply settings to globals
            globals()['hardlock'] = bool(hardlock_var.get())
            globals()['USE_OS_LOCK'] = bool(use_os_lock_var.get())
            globals()['LOCK_SECONDS'] = max(60, val)
            globals()['run_on_startup'] = bool(run_startup_var.get())
            globals()['selected_country'] = c
            globals()['selected_state'] = s
            globals()['selected_city'] = ci
            save_config()
            try:
                conf.destroy()
            except Exception:
                pass
            try:
                root.quit()
                root.destroy()
            except Exception:
                pass

        tk.Button(btn_frame, text="Back", width=12, command=on_back).pack(side="left", padx=10)
        tk.Button(btn_frame, text="Confirm", width=12, command=on_confirm).pack(side="right", padx=10)
        conf.grab_set()
        root.wait_window(conf)

    def on_ok():
        if not (root and getattr(root, "winfo_exists", None) and root.winfo_exists()):
            return
        c = safe_get_str(country_entry).strip()
        s = safe_get_str(state_entry).strip()
        ci = safe_get_str(city_entry).strip()
        if not (c and s and ci):
            try:
                messagebox.showwarning("Incomplete", "Please select country, state and city.")
            except Exception:
                pass
            return
        if not validate_selection(c, s, ci, city_entry):
            return
        prev_vals = (selected_country, selected_state, selected_city)
        # set selected globals for fetch
        globals()['selected_country'] = c
        globals()['selected_state'] = s
        globals()['selected_city'] = ci
        # fetch prayer times now (will fall back to defaults on failure)
        try:
            fetch_prayer_times(c, ci)
        except Exception:
            log.exception("fetch_prayer_times failed")
        show_confirmation_window(prev_vals, c, s, ci)

    ttk.Button(root, text="OK", command=on_ok).pack(pady=14)

    # If we already had settings, pre-populate PRAYER_SCHEDULE for preview
    if not selected_country or not selected_city:
        PRAYER_SCHEDULE = {"fajr": (6, 0), "dhuhr": (13, 0), "asr": (15, 45), "maghrib": (18, 15), "isha": (20, 0)}
    else:
        try:
            fetch_prayer_times()
        except Exception:
            PRAYER_SCHEDULE = {"fajr": (6, 0), "dhuhr": (13, 0), "asr": (15, 45), "maghrib": (18, 15), "isha": (20, 0)}

    root.mainloop()

# Reminder popup (kept simple)
def _show_reminder_popup(name: str):
    try:
        root = tk.Tk()
        root.title("Prayer Reminder")
        root.geometry("420x200")
        root.attributes("-topmost", True)
        root.configure(bg="#111")
        tk.Label(root, text=f"Time for {name.title()}", font=("Helvetica", 20), bg="#111", fg="#ffd166").pack(pady=14)
        tk.Label(root, text="Will lock PC in a few seconds.", font=("Helvetica", 12), bg="#111", fg="#eee").pack(pady=8)
        tk.Button(root, text="I Understand", font=("Helvetica", 12), command=root.destroy).pack(pady=14)
        root.mainloop()
    except Exception:
        log.exception("Reminder popup failed")

# Main loop
def main_loop():
    start_tray_icon()
    fetch_prayer_times()
    global prayed_today
    prayed_today = {name: False for name in PRAYER_SCHEDULE}
    try:
        while True:
            reset_if_new_day()
            now = datetime.now()
            times = today_prayer_times()
            for name, ptime in times.items():
                if name not in prayed_today:
                    prayed_today[name] = False
                if not prayed_today.get(name, False):
                    # show reminder popup in a separate thread
                    if ptime <= now < ptime + timedelta(seconds=120):
                        threading.Thread(target=lambda n=name: _show_reminder_popup(n), daemon=True).start()
                    # lock shortly after the prayer time
                    if ptime <= now < ptime + timedelta(seconds=60):
                        prayed_today[name] = True
                        lock_now()
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted by user. Exiting.")
        try:
            unlock_lock_screen()
        except Exception:
            pass

if __name__ == "__main__":
    if os.path.exists(CONFIG_PATH):
        if colorama:
            print(colorama.Fore.GREEN + f"Loading config from {CONFIG_PATH}" + colorama.Style.RESET_ALL)
        load_config()
        try:
            minimize_console()
        except Exception:
            pass
    else:
        if os.path.exists(LEGACY_CONFIG_PATH):
            if colorama:
                print(colorama.Fore.YELLOW + f"Migrating legacy config and loading." + colorama.Style.RESET_ALL)
            load_config()
        else:
            if colorama:
                print(colorama.Fore.YELLOW + f"No config found, showing setup." + colorama.Style.RESET_ALL)
            show_setup()
    main_loop()