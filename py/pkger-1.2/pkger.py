#!/usr/bin/env python3
"""
PKGER — desktop package manager for Arch Linux and Arch-based systems.
Modern GTK4/Adwaita sidebar UI with pacman/AUR backend.
Optimized for Nuitka (Standalone/Onefile) & Python 3.13/3.14 compatibility.
"""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# --- Nuitka & Environment Fixes ---
IS_NUITKA = "__compiled__" in globals()
PKGER_DEBUG = (os.environ.get("PKGER_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")

def _configure_gtk_runtime():
    """GTK/GSK defaults that avoid EGL/portal crashes on common X11 setups."""
    # GskGLRenderer on GdkX11Popup (tooltips, menus, file chooser) segfaults when EGL
    # is missing. Cairo is slower but stable; set PKGER_GSK_RENDERER=ngl to opt in.
    if "GSK_RENDERER" not in os.environ:
        os.environ["GSK_RENDERER"] = os.environ.get("PKGER_GSK_RENDERER", "cairo")
    # FileChooserNative + xdg-desktop-portal can hit g_dbus_connection_send_message asserts.
    if "GTK_USE_PORTAL" not in os.environ:
        use_portal = (os.environ.get("PKGER_USE_PORTAL") or "0").strip().lower()
        os.environ["GTK_USE_PORTAL"] = "1" if use_portal in ("1", "true", "yes", "on") else "0"
    # Do not force GDK_DEBUG=portals (noisy and can worsen portal popups).


_configure_gtk_runtime()

def log_debug(msg):
    if PKGER_DEBUG:
        print(f"[DEBUG-NUITKA] {msg}", file=sys.stderr)


def _prepend_env_path(var_name, path):
    """Prepend a directory to a colon-separated environment variable."""
    if not path:
        return
    p = str(path)
    if not os.path.isdir(p):
        return
    current = os.environ.get(var_name, "")
    if current:
        if p not in current.split(":"):
            os.environ[var_name] = f"{p}:{current}"
    else:
        os.environ[var_name] = p


def _nuitka_bundle_dir():
    """Directory where Nuitka unpacked the standalone payload (onefile or dist)."""
    candidates = []
    try:
        candidates.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    for key in (
        "NUITKA_ONEFILE_DIRECTORY",
        "NUITKA_PACKAGE_HOME",
    ):
        val = os.environ.get(key)
        if val:
            candidates.append(Path(val))
    try:
        compiled = globals().get("__compiled__")
        if compiled is not None and hasattr(compiled, "containing_dir"):
            candidates.append(Path(compiled.containing_dir))
    except Exception:
        pass
    try:
        candidates.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass

    def _looks_like_bundle(p):
        if not p or not p.is_dir():
            return False
        return any(
            (p / name).exists()
            for name in (
                "girepository-1.0",
                "lib",
                "share",
                "gtk4",
                "pkger.bin",
                "pkger",
            )
        )

    for p in candidates:
        if _looks_like_bundle(p):
            return p
    for p in candidates:
        if p.is_dir():
            return p
    return Path.cwd()


def _nuitka_preload_glib(bundle_dir):
    """Load bundled GLib/GObject before PyGObject (helps onefile/standalone)."""
    if os.name != "posix":
        return
    lib_dir = bundle_dir / "lib"
    if not lib_dir.is_dir():
        return
    try:
        import ctypes
    except ImportError:
        return
    for pattern in (
        "libglib-2.0.so*",
        "libgobject-2.0.so*",
        "libgio-2.0.so*",
        "libgmodule-2.0.so*",
        "libgirepository-*.so*",
    ):
        for so_path in sorted(lib_dir.glob(pattern)):
            try:
                ctypes.CDLL(str(so_path), mode=ctypes.RTLD_GLOBAL)
                log_debug(f"Preloaded {so_path.name}")
            except OSError as exc:
                log_debug(f"Preload skip {so_path}: {exc}")


def _setup_nuitka_environment():
    if not IS_NUITKA:
        return
    try:
        bundle_dir = _nuitka_bundle_dir()
        log_debug(f"Nuitka bundle dir: {bundle_dir}")
        if str(bundle_dir) not in sys.path:
            sys.path.insert(0, str(bundle_dir))

        typelib_path = bundle_dir / "girepository-1.0"
        if typelib_path.is_dir():
            _prepend_env_path("GI_TYPELIB_PATH", typelib_path)

        lib_path = bundle_dir / "lib"
        if lib_path.is_dir():
            _prepend_env_path("LD_LIBRARY_PATH", lib_path)

        share_dir = bundle_dir / "share"
        if share_dir.is_dir():
            _prepend_env_path("XDG_DATA_DIRS", share_dir)
            schemas = share_dir / "glib-2.0" / "schemas"
            if schemas.is_dir():
                os.environ.setdefault(
                    "GSETTINGS_SCHEMA_DIR",
                    str(schemas),
                )
            icons = share_dir / "icons"
            if icons.is_dir():
                os.environ.setdefault("GTK_PATH", str(share_dir))

        _nuitka_preload_glib(bundle_dir)
    except Exception as exc:
        log_debug(f"Nuitka env setup error: {exc}")


_setup_nuitka_environment()

# Check for help/version before heavy imports
if any(arg in ("--version", "-V") for arg in sys.argv[1:]):
    print("PKGER v1.2")
    sys.exit(0)

try:
    import gi

    try:
        gi.require_version("Gtk", "4.0")
    except Exception:
        pass
    try:
        gi.require_version("Adw", "1")
    except Exception:
        pass

    from gi.repository import Gtk as Gtk4
    from gi.repository import Gdk as Gdk4
    from gi.repository import Gio as Gio4
    from gi.repository import GObject as GObject4
    from gi.repository import GLib
    try:
        from gi.repository import Adw
    except ImportError:
        Adw = None

    GTK4_AVAILABLE = True
except Exception as e:
    GTK4_AVAILABLE = False
    log_debug(f"GTK4 Load Error: {e}")
    if PKGER_DEBUG:
        import traceback
        traceback.print_exc()
    print(f"Fatal: GTK4/PyGObject not available: {e}", file=sys.stderr)
    if IS_NUITKA:
        print(
            "Nuitka build hint: use build-nuitka.sh (needs --enable-plugin=gi). "
            "Debug: PKGER_DEBUG=1 ./pkger.bin",
            file=sys.stderr,
        )
    sys.exit(1)

# AppIndicator support
APPINDICATOR_AVAILABLE = False
AppIndicator3 = None
try:
    for name in ["AyatanaAppIndicator3", "AppIndicator3"]:
        try:
            if not hasattr(sys, "frozen"): gi.require_version(name, "0.1")
            AppIndicator3 = __import__(f"gi.repository.{name}", fromlist=[name])
            APPINDICATOR_AVAILABLE = True
            break
        except: continue
except: pass


def gtk4_run_open_file(parent, title, patterns, on_path):
    """Pick one file; call on_path(path_or_none) on the GTK main loop."""
    patterns = [p for p in (patterns or ["*"]) if p]

    def deliver(path):
        if callable(on_path):
            GLib.idle_add(on_path, path)

    if hasattr(Gtk4, "FileDialog"):
        dialog = Gtk4.FileDialog()
        dialog.set_title(title or "Open file")
        filt = Gtk4.FileFilter()
        filt.set_name("Files")
        for pat in patterns:
            filt.add_pattern(pat)
        dialog.set_default_filter(filt)

        def _done(_dlg, result):
            path = None
            try:
                gfile = _dlg.open_finish(result)
                if gfile is not None:
                    path = gfile.get_path()
            except GLib.Error:
                pass
            deliver(path)

        try:
            dialog.open(parent, None, _done)
            return
        except Exception as exc:
            log_debug(f"Gtk.FileDialog.open failed: {exc}")

    dialog = Gtk4.FileChooserDialog(
        title=title or "Open file",
        transient_for=parent,
        modal=True,
        action=Gtk4.FileChooserAction.OPEN,
    )
    dialog.add_buttons("_Cancel", Gtk4.ResponseType.CANCEL, "_Open", Gtk4.ResponseType.ACCEPT)
    filt = Gtk4.FileFilter()
    filt.set_name("Files")
    for pat in patterns:
        filt.add_pattern(pat)
    dialog.set_filter(filt)

    def _response(dlg, response):
        path = None
        if response == Gtk4.ResponseType.ACCEPT:
            gfile = dlg.get_file()
            if gfile is not None:
                path = gfile.get_path()
        dlg.destroy()
        deliver(path)

    dialog.connect("response", _response)
    dialog.present()


def gtk4_run_save_file(parent, title, default_name, on_path):
    """Pick save path; call on_path(path_or_none) on the GTK main loop."""
    default_name = (default_name or "export.txt").strip()

    def deliver(path):
        if callable(on_path):
            GLib.idle_add(on_path, path)

    if hasattr(Gtk4, "FileDialog"):
        dialog = Gtk4.FileDialog()
        dialog.set_title(title or "Save file")
        if hasattr(dialog, "set_initial_name"):
            dialog.set_initial_name(default_name)

        def _done(_dlg, result):
            path = None
            try:
                gfile = _dlg.save_finish(result)
                if gfile is not None:
                    path = gfile.get_path()
            except GLib.Error:
                pass
            deliver(path)

        try:
            dialog.save(parent, None, _done)
            return
        except Exception as exc:
            log_debug(f"Gtk.FileDialog.save failed: {exc}")

    dialog = Gtk4.FileChooserDialog(
        title=title or "Save file",
        transient_for=parent,
        modal=True,
        action=Gtk4.FileChooserAction.SAVE,
    )
    dialog.add_buttons("_Cancel", Gtk4.ResponseType.CANCEL, "_Save", Gtk4.ResponseType.ACCEPT)
    dialog.set_current_name(default_name)

    def _response(dlg, response):
        path = None
        if response == Gtk4.ResponseType.ACCEPT:
            gfile = dlg.get_file()
            if gfile is not None:
                path = gfile.get_path()
        dlg.destroy()
        deliver(path)

    dialog.connect("response", _response)
    dialog.present()



APP_NAME = "PKGER v1.2"
APP_VERSION = "1.2"
__version__ = APP_VERSION
APP_DEVELOPER = "almezali"
APP_YEAR = "2026"

log = logging.getLogger(__name__)

ARCH_NEWS_RSS = "https://archlinux.org/feeds/news/"

CONFIG_DIR = Path.home() / ".config" / "pkger"
CONFIG_FILE = CONFIG_DIR / "settings.json"
HISTORY_FILE = CONFIG_DIR / "history.json"
FAVORITES_FILE = CONFIG_DIR / "favorites.json"

DEFAULT_SETTINGS = {
    "auto_check_updates": True,
    "show_security_alerts": True,
    "search_official": True,
    "search_aur": True,
    "search_flatpak": True,
    "search_appimage": True,
    "theme_accent": "default",
    "confirm_install": True,
    "confirm_remove": True,
    "max_search_results": 80,
    "language": "auto",
}

NEWS_RSS = ARCH_NEWS_RSS


def detect_distro():
    info = {"id": "arch", "name": "Arch Linux", "version": "", "codename": ""}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    v = v.strip('"')
                    if k == "ID":
                        info["id"] = v.lower()
                    elif k == "NAME":
                        info["name"] = v
                    elif k == "VERSION_ID":
                        info["version"] = v
                    elif k == "VERSION_CODENAME":
                        info["codename"] = v
        lid = info["id"]
        arch_ids = ("arch", "archlinux", "manjaro", "endeavouros", "garuda", "cachyos", "artix")
        if any(x in lid for x in arch_ids):
            if "manjaro" in lid:
                info["id"] = "manjaro"
            elif "endeavour" in lid:
                info["id"] = "endeavouros"
            else:
                info["id"] = "arch"
    except Exception:
        pass
    return info


DISTRO_INFO = detect_distro()
DISTRO = DISTRO_INFO["id"]


class AppConfig:
    _lock = threading.Lock()

    @classmethod
    def load(cls):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = dict(DEFAULT_SETTINGS)
        try:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE) as f:
                    data.update(json.load(f))
        except Exception:
            pass
        return data

    @classmethod
    def save(cls, settings):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with cls._lock:
            with open(CONFIG_FILE, "w") as f:
                json.dump(settings, f, indent=2)

    @classmethod
    def load_json(cls, path, default=None):
        try:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        except Exception:
            pass
        return default if default is not None else []

    @classmethod
    def save_json(cls, path, data):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with cls._lock:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)


def human_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def get_pacman_cache_size():
    total = 0
    for d in ("/var/cache/pacman/pkg",):
        p = Path(d)
        if p.is_dir():
            try:
                for f in p.rglob("*"):
                    if f.is_file():
                        total += f.stat().st_size
            except Exception:
                pass
    return total


def get_system_stats():
    stats = {
        "kernel": subprocess.getoutput("uname -r"),
        "uptime": "-",
        "load": "-",
        "disk_free": "-",
        "mem_used_pct": "-",
        "cache_size": get_pacman_cache_size(),
    }
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
            days, rem = divmod(int(secs), 86400)
            hrs, rem = divmod(rem, 3600)
            mins = rem // 60
            stats["uptime"] = f"{days}d {hrs}h {mins}m" if days else f"{hrs}h {mins}m"
    except Exception:
        pass
    try:
        load = subprocess.getoutput("cat /proc/loadavg").split()[:3]
        stats["load"] = " ".join(load)
    except Exception:
        pass
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if len(lines) > 1:
            stats["disk_free"] = lines[1].split()[3]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, v = line.split(":", 1)
                mem[k.strip()] = int(v.strip().split()[0])
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        if total:
            stats["mem_used_pct"] = f"{100 - int(avail * 100 / total)}%"
    except Exception:
        pass
    return stats


def classify_update_severity(name, repository, source, severity="-", kind="-"):
    name_l = (name or "").lower()
    if any(k in name_l for k in ("linux", "linux-lts", "linux-zen", "linux-hardened", "kernel")):
        return "kernel"
    if any(k in name_l for k in ("openssl", "openssh", "sudo", "polkit", "glibc", "systemd")):
        return "security"
    src = (source or "").lower()
    if src == "flatpak":
        return "important"
    return "normal"


def compute_health_score(installed_count, updates, held, stats):
    score = 100
    security = sum(
        1 for u in (updates or [])
        if classify_update_severity(
            u.get("name"), u.get("repository"), u.get("source"),
            u.get("severity"), u.get("kind"),
        ) == "security"
    )
    kernel = sum(
        1 for u in (updates or [])
        if classify_update_severity(
            u.get("name"), u.get("repository"), u.get("source"),
            u.get("severity"), u.get("kind"),
        ) == "kernel"
    )
    if security:
        score -= min(30, security * 8)
    if kernel:
        score -= min(15, kernel * 5)
    n_upd = len(updates or [])
    if n_upd > 50:
        score -= 10
    elif n_upd > 20:
        score -= 5
    if held:
        score -= min(10, len(held) * 2)
    cache_gb = stats.get("cache_size", 0) / (1024 ** 3)
    if cache_gb > 2:
        score -= 10
    elif cache_gb > 1:
        score -= 5
    return max(0, min(100, score))


SEVERITY_LABELS = {
    "security": "Security",
    "kernel": "Kernel",
    "important": "Important",
    "normal": "Update",
}


def _is_security_update(item):
    return classify_update_severity(
        item.get("name"),
        item.get("repository"),
        item.get("source"),
        item.get("severity"),
        item.get("kind"),
    ) == "security"

def fuzzy_match(pattern, text):
    """Simple fuzzy matching: checks if pattern characters appear in text in order."""
    p_idx = 0
    t_idx = 0
    while p_idx < len(pattern) and t_idx < len(text):
        if pattern[p_idx].lower() == text[t_idx].lower():
            p_idx += 1
        t_idx += 1
    return p_idx == len(pattern)

def _configure_debug_logging():
    """Optional stderr diagnostics when PKGER_DEBUG is set (non-fatal if unused)."""
    flag = (os.environ.get("PKGER_DEBUG") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    if log.handlers:
        log.setLevel(logging.DEBUG)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s [pkger] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)


def fetch_arch_news_items(limit=20):
    """Parse Arch Linux news RSS (channel/item)."""
    try:
        req = urllib.request.Request(
            ARCH_NEWS_RSS,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION} (+Arch Linux feed)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        items = []
        channel = root.find("channel")
        if channel is None:
            return items
        for item in channel.findall("item")[:limit]:
            def _txt(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""

            items.append(
                {
                    "title": _txt("title"),
                    "link": _txt("link"),
                    "summary": _txt("description"),
                    "pub_date": _txt("pubDate"),
                }
            )
        return items
    except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError, ValueError):
        return []


# ----------------------------
# Utility helpers
# ----------------------------

def run_command_stream(command, on_output, on_progress_hint=None, password=None):
    """Run a command and stream stdout lines via on_output callback.
    If password is set, runs under sudo -S (stdin). Supports argv lists and
    ['bash', '-c', 'script with | pipes'] for safe piped pacman operations.
    """
    try:
        proc = None
        if password and PRIV_TOOL == "sudo" and command and command[0] != "sudo":
            if len(command) >= 3 and command[0] == "bash" and command[1] == "-c":
                proc = subprocess.Popen(
                    ["sudo", "-S", "bash", "-c", command[2]],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                proc.stdin.write(password + "\n")
                proc.stdin.close()
            else:
                joined = shlex.join(command)
                full = ["bash", "-lc", f"echo {shlex.quote(password)} | sudo -S {joined}"]
                proc = subprocess.Popen(
                    full,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )
        elif password and PRIV_TOOL == "doas" and command and command[0] != "doas":
            run_cmd = command
            if len(command) >= 3 and command[0] == "bash" and command[1] == "-c":
                run_cmd = ["doas", "bash", "-c", command[2]]
            else:
                run_cmd = ["doas"] + command
            proc = subprocess.Popen(
                run_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
        else:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

        while True:
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if line:
                on_output(line.rstrip())
                if on_progress_hint:
                    on_progress_hint(line)
        return proc.wait()
    except Exception as e:
        on_output(f"ERROR: {e}")
        return 1


def command_exists(cmd):
    try:
        subprocess.run([cmd, '--version'], capture_output=True, check=False)
        return True
    except Exception:
        return False


def detect_privilege_tool():
    if command_exists("sudo"):
        return "sudo"
    if command_exists("doas"):
        return "doas"
    return None


def detect_aur_helper():
    if command_exists("yay"):
        return "yay"
    if command_exists("paru"):
        return "paru"
    return None


PRIV_TOOL = detect_privilege_tool()
AUR_HELPER = detect_aur_helper()
SEARCH_CACHE = {}
SEARCH_CACHE_TTL_SECONDS = 45
SEARCH_CACHE_MAX_ITEMS = 100


def scan_appimages(search_query=""):
    locations = [
        Path.home() / "Applications",
        Path.home() / ".local" / "bin",
        Path("/opt"),
    ]
    q = (search_query or "").strip().lower()
    out = []
    for loc in locations:
        if not loc.exists():
            continue
        try:
            for p in loc.rglob("*.AppImage"):
                name = p.stem
                if q and q not in name.lower():
                    continue
                out.append(
                    {
                        "name": name,
                        "pkg": name,
                        "version": "-",
                        "description": f"Local AppImage: {p}",
                        "repository": "appimage",
                        "installed": True,
                    }
                )
        except Exception:
            continue
    return out


INSTALL_SUBSET_KEYS = frozenset(
    {"installed_native", "installed_foreign", "installed_flatpak", "installed_appimage"}
)

# GTK Install combobox labels → unified_search_packages search_type keys
INSTALL_COMBO_TO_KEY = {
    "Unified": "unified",
    "Official": "official",
    "AUR": "aur",
    "Installed (search)": "installed",
    "Installed (native)": "installed_native",
    "Installed (foreign)": "installed_foreign",
    "Installed (Flatpak)": "installed_flatpak",
    "Installed (AppImage)": "installed_appimage",
    "Flatpak": "flatpak",
    "AppImage": "appimage",
    "Developer (packager)": "developer",
}

GTK4_INSTALL_MODE_KEYS = [
    "unified",
    "official",
    "aur",
    "installed",
    "installed_native",
    "installed_foreign",
    "installed_flatpak",
    "installed_appimage",
    "flatpak",
    "appimage",
    "developer",
]


def fetch_installed_subset_packages(mode_key, filter_q=""):
    """List installed packages for a specific origin (native repos, foreign, Flatpak, AppImage)."""
    q = (filter_q or "").strip().lower()
    out = []

    def match_item(it):
        if not q:
            return True
        hay = " ".join(
            str(it.get(k, ""))
            for k in ("name", "pkg", "version", "description", "repository")
        ).lower()
        return q in hay

    try:
        if mode_key == "installed_native":
            r = subprocess.run(["pacman", "-Qen"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return []
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    it = {
                        "name": parts[0],
                        "pkg": parts[0],
                        "version": parts[1],
                        "description": "Explicit package from sync repositories",
                        "repository": "native",
                        "installed": True,
                    }
                    if match_item(it):
                        out.append(it)
        elif mode_key == "installed_foreign":
            r = subprocess.run(["pacman", "-Qm"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return []
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    it = {
                        "name": parts[0],
                        "pkg": parts[0],
                        "version": parts[1],
                        "description": "Foreign package (AUR / local PKGBUILD / third-party)",
                        "repository": "foreign",
                        "installed": True,
                    }
                    if match_item(it):
                        out.append(it)
        elif mode_key == "installed_flatpak":
            if not command_exists("flatpak"):
                return []
            r = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application,version,origin,branch"],
                capture_output=True,
                text=True,
                timeout=90,
            )
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line or line.startswith("No ") or line.startswith("Append"):
                    continue
                if "\t" in line:
                    parts = [p.strip() for p in line.split("\t") if p.strip()]
                else:
                    parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
                if not parts:
                    continue
                app_id = parts[0]
                ver = parts[1] if len(parts) > 1 else "-"
                origin = parts[2] if len(parts) > 2 else ""
                br = parts[3] if len(parts) > 3 else ""
                desc = f"Flatpak app — origin: {origin or '?'}" + (f", branch: {br}" if br else "")
                it = {
                    "name": app_id,
                    "pkg": app_id,
                    "version": ver,
                    "description": desc,
                    "repository": "flatpak",
                    "installed": True,
                }
                if match_item(it):
                    out.append(it)
        elif mode_key == "installed_appimage":
            for it in scan_appimages(q if q else ""):
                it = dict(it)
                it["installed"] = True
                if not q or match_item(it):
                    out.append(it)
    except Exception:
        return []
    return out


def search_developer_packagers(query):
    """Match packager/maintainer text using expac; supplement with paru --by maintainer when available."""
    ql = (query or "").strip().lower()
    if not ql:
        return []
    installed_names = set()
    try:
        rq = subprocess.run(["pacman", "-Qq"], capture_output=True, text=True, timeout=25)
        if rq.returncode == 0:
            installed_names = {ln.strip() for ln in rq.stdout.splitlines() if ln.strip()}
    except Exception:
        pass

    seen = set()
    rows = []

    def add_row(name, ver, pack, repo_tag):
        if not name or name in seen:
            return
        p = (pack or "").strip()
        if ql not in p.lower():
            return
        seen.add(name)
        short = p.replace("\n", " ").strip()
        if len(short) > 220:
            short = short[:217] + "..."
        rows.append(
            {
                "name": name,
                "pkg": name,
                "version": ver or "-",
                "description": f"Packager / maintainer: {short}",
                "repository": repo_tag,
                "installed": name in installed_names,
            }
        )

    if command_exists("expac"):
        for flag, tag in (("-Q", "installed"), ("-S", "sync")):
            try:
                r = subprocess.run(
                    ["expac", flag, "%n\t%v\t%p"],
                    capture_output=True,
                    text=True,
                    timeout=240 if flag == "-S" else 90,
                )
                for line in (r.stdout or "").splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        add_row(parts[0], parts[1], parts[2], tag)
            except Exception:
                continue

    if AUR_HELPER == "paru" and command_exists("paru"):
        try:
            r = subprocess.run(
                ["paru", "-Ss", "--by", "maintainer", query],
                capture_output=True,
                text=True,
                timeout=90,
            )
            for line in (r.stdout or "").splitlines():
                if not line.strip() or line.startswith(" "):
                    continue
                parts = line.split(" ", 2)
                if len(parts) < 2:
                    continue
                name = parts[1] if parts[0] == "aur/" else parts[0]
                version = parts[1] if parts[0] == "aur/" else ""
                description = parts[2] if len(parts) > 2 else ""
                if name in seen:
                    continue
                seen.add(name)
                rows.append(
                    {
                        "name": name,
                        "pkg": name,
                        "version": version or "-",
                        "description": description or "paru maintainer search",
                        "repository": "aur",
                        "installed": name in installed_names,
                    }
                )
        except Exception:
            pass

    rows.sort(key=lambda x: (x.get("name") or "").lower())
    return rows


def unified_search_packages(query, search_type):
    """Unified package search across official/AUR/installed/Flatpak/AppImage."""
    st = (search_type or "official").lower()
    q = (query or "").strip()
    ql = q.lower()
    cache_key = (st, ql)
    now = time.time()
    cached = SEARCH_CACHE.get(cache_key)
    if cached and (now - cached["ts"] <= SEARCH_CACHE_TTL_SECONDS):
        return list(cached["results"])

    if st == "developer":
        if not ql:
            return []
        res = search_developer_packagers(q)
        SEARCH_CACHE[cache_key] = {"ts": now, "results": list(res)}
        return list(res)

    if st in INSTALL_SUBSET_KEYS:
        res = fetch_installed_subset_packages(st, q)
        SEARCH_CACHE[cache_key] = {"ts": now, "results": list(res)}
        return list(res)

    if st in ("unified", "official", "aur", "flatpak") and not ql:
        return []

    query_tokens = [t for t in re.split(r"\s+", ql) if t]
    installed_names = set()
    try:
        rq = subprocess.run(["pacman", "-Qq"], capture_output=True, text=True, timeout=25)
        if rq.returncode == 0:
            installed_names = {ln.strip() for ln in rq.stdout.splitlines() if ln.strip()}
    except Exception:
        installed_names = set()

    keyword_hints = {
        "browser": ["browser", "web"],
        "web": ["browser", "web"],
        "music": ["audio", "player", "music"],
        "player": ["player", "media"],
        "games": ["game", "gaming"],
        "youtube": ["youtube", "yt", "video"],
        "soundcloud": ["soundcloud", "music", "audio"],
    }
    expanded_tokens = list(query_tokens)
    for t in query_tokens:
        for hint in keyword_hints.get(t, []):
            if hint not in expanded_tokens:
                expanded_tokens.append(hint)

    def parse_pacman_search(output, source_hint="official"):
        packages = []
        lines = output.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line and not line.startswith(" "):
                parts = line.split(" ", 1)
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1] if len(parts) > 1 else ""
                    description = ""
                    if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                        description = lines[i + 1].strip()
                        i += 1
                    repo = name.split("/")[0] if "/" in name else "unknown"
                    pkg_name = name.split("/")[-1]
                    packages.append(
                        {
                            "name": name,
                            "pkg": pkg_name,
                            "version": version,
                            "description": description,
                            "repository": repo if source_hint == "official" else source_hint,
                            "installed": pkg_name in installed_names,
                        }
                    )
            i += 1
        return packages

    def parse_aur_search(output):
        packages = []
        for line in output.split("\n"):
            if line.strip() and not line.startswith(" "):
                parts = line.split(" ", 2)
                if len(parts) >= 2:
                    name = parts[1] if parts[0] == "aur/" else parts[0]
                    version = parts[1] if parts[0] == "aur/" else ""
                    description = parts[2] if len(parts) > 2 else ""
                    packages.append(
                        {
                            "name": name,
                            "pkg": name,
                            "version": version,
                            "description": description,
                            "repository": "aur",
                            "installed": name in installed_names,
                        }
                    )
        return packages

    def parse_flatpak_search(output):
        packages = []
        for line in output.splitlines():
            txt = line.strip()
            if not txt:
                continue
            parts = [p.strip() for p in txt.split("\t") if p.strip()]
            name = parts[0] if parts else txt
            app_id = parts[1] if len(parts) > 1 else name
            desc = parts[2] if len(parts) > 2 else ""
            packages.append(
                {
                    "name": app_id,
                    "pkg": app_id,
                    "version": "-",
                    "description": desc or f"Flatpak app: {name}",
                    "repository": "flatpak",
                    "installed": False,
                }
            )
        return packages

    def _search_official():
        try:
            r = subprocess.run(["pacman", "-Ss", q], capture_output=True, text=True, timeout=35)
            return parse_pacman_search(r.stdout, "official")
        except Exception:
            return []

    def _search_installed():
        try:
            cmd = ["pacman", "-Qs", q] if q else ["pacman", "-Q"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            return parse_pacman_search(r.stdout, "installed")
        except Exception:
            return []

    def _search_aur():
        if not AUR_HELPER:
            return []
        try:
            r = subprocess.run([AUR_HELPER, "-Ss", q], capture_output=True, text=True, timeout=45)
            return parse_aur_search(r.stdout)
        except Exception:
            return []

    def _search_flatpak():
        if not command_exists("flatpak"):
            return []
        try:
            r = subprocess.run(
                ["flatpak", "search", "--columns=name,application,description", q],
                capture_output=True,
                text=True,
                timeout=50,
            )
            return parse_flatpak_search(r.stdout)
        except Exception:
            return []

    def _search_appimage():
        return scan_appimages(ql)

    jobs = []
    if st in ("official", "all", "unified"):
        jobs.append(_search_official)
    if st in ("installed", "all", "unified"):
        jobs.append(_search_installed)
    if st in ("aur", "all", "unified"):
        jobs.append(_search_aur)
    if st in ("flatpak", "all", "unified"):
        jobs.append(_search_flatpak)
    if st in ("appimage", "all", "unified"):
        jobs.append(_search_appimage)

    merged = []
    seen = set()
    with ThreadPoolExecutor(max_workers=max(1, min(5, len(jobs)))) as ex:
        for sub in ex.map(lambda fn: fn(), jobs):
            for it in sub:
                key = (it.get("repository", ""), it.get("pkg", it.get("name", "")))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(it)

    # Keyword matching in name + description with relevance score
    scored = []
    for it in merged:
        name = (it.get("name") or it.get("pkg") or "").lower()
        desc = (it.get("description") or "").lower()
        repo = (it.get("repository") or "").lower()
        hay = f"{name} {desc} {repo}"
        score = 0
        if ql and ql in hay:
            score += 50
        elif ql and fuzzy_match(ql, name):
            score += 25
        for tok in expanded_tokens:
            if tok in name:
                score += 12
            elif tok in desc:
                score += 8
            elif tok in repo:
                score += 4
        if ql and score == 0:
            continue
        if it.get("installed"):
            score += 1
        it["_score"] = score
        scored.append(it)

    scored.sort(key=lambda x: (x.get("_score", 0), x.get("name", "").lower()), reverse=True)
    for it in scored:
        it.pop("_score", None)

    SEARCH_CACHE[cache_key] = {"ts": now, "results": list(scored)}
    if len(SEARCH_CACHE) > SEARCH_CACHE_MAX_ITEMS:
        old_keys = sorted(SEARCH_CACHE.keys(), key=lambda k: SEARCH_CACHE[k]["ts"])
        for k in old_keys[: max(1, len(old_keys) - SEARCH_CACHE_MAX_ITEMS)]:
            SEARCH_CACHE.pop(k, None)
    return scored


def get_pacman_repo_allowlist():
    """Names of configured sync databases (filters stray stdout from wrappers)."""
    try:
        r = subprocess.run(
            ["pacman-conf", "--repo-list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0:
            return frozenset(line.strip() for line in r.stdout.splitlines() if line.strip())
    except Exception as exc:
        log.debug("pacman-conf --repo-list failed: %s", exc, exc_info=True)
    return None


_REPO_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_.\[\]]{0,127}$")


def _is_plausible_repo_name(token):
    """Fallback when pacman-conf is unavailable."""
    if not token or not _REPO_TOKEN_RE.match(token):
        return False
    if token[0].isdigit():
        return False
    return True


def classify_repo_kind(repo_id):
    """Classify pacman repository IDs for UI filtering."""
    rid = (repo_id or "").strip().lower()
    if rid in ("core", "extra", "multilib", "community"):
        return "official"
    if any(k in rid for k in ("chaotic", "blackarch", "archlinuxcn")):
        return "third-party"
    if "testing" in rid:
        return "testing"
    return "community"


def get_repository_presets():
    """Common third-party repository setup hints (manual pacman.conf edits)."""
    return [
        {
            "title": "Chaotic-AUR (helper script)",
            "description": "Install chaotic-aur keyring and mirrorlist via official guide script.",
            "cmd": [
                "bash",
                "-lc",
                "curl -fsSL https://geo-mirror.chaotic.cx/chaotic-aur/chaotic-keyring.pkg.tar.zst "
                "-o /tmp/chaotic-keyring.pkg.tar.zst && "
                "curl -fsSL https://geo-mirror.chaotic.cx/chaotic-aur/chaotic-mirrorlist.pkg.tar.zst "
                "-o /tmp/chaotic-mirrorlist.pkg.tar.zst && "
                "pacman -U --noconfirm /tmp/chaotic-keyring.pkg.tar.zst /tmp/chaotic-mirrorlist.pkg.tar.zst",
            ],
        },
    ]


def build_addrepo_from_url_command(repo_url, repo_id=None):
    """Download a mirrorlist/include file into /etc/pacman.d/."""
    url = (repo_url or "").strip()
    if not url:
        return None
    if url.startswith("/") or url.startswith("file://"):
        path = url[7:] if url.startswith("file://") else url
        dest = f"/etc/pacman.d/{Path(path).name}"
        return ["bash", "-lc", f"install -m 644 {shlex.quote(path)} {shlex.quote(dest)}"]
    if not url.startswith(("http://", "https://")):
        return None
    name = (repo_id or Path(url.split("?")[0]).name or "custom-mirrorlist").strip()
    dest = f"/etc/pacman.d/{name}"
    return [
        "bash",
        "-lc",
        f"curl -fsSL {shlex.quote(url)} -o {shlex.quote(dest)} && chmod 644 {shlex.quote(dest)}",
    ]


def build_addrepo_manual_command(repo_id, name, server_url, gpgcheck=True, gpgkey="", enabled=True):
    """Append a [repo] section to /etc/pacman.conf."""
    rid = (repo_id or "").strip()
    if not rid or not (server_url or "").strip():
        return None
    su = server_url.strip()
    if su.startswith("baseurl="):
        su = su.split("=", 1)[1].strip()
    if su.startswith("Server="):
        su = su.split("=", 1)[1].strip()
    block = (
        f"\\n[{rid}]\\n"
        f"SigLevel = {'Required DatabaseOptional' if gpgcheck else 'Never'}\\n"
        f"Server = {su}\\n"
    )
    if gpgkey:
        gk = gpgkey.strip()
        block += f"# Key: {gk}\\n"
    if not enabled:
        block += "# Disabled — comment out or remove this section to keep it unused.\\n"
    return [
        "bash",
        "-lc",
        f"printf '%b' {shlex.quote(block)} >> /etc/pacman.conf",
    ]


def build_aur_install_command(package_name):
    """Install an AUR package by name using yay or paru."""
    spec = (package_name or "").strip()
    if not spec or not AUR_HELPER:
        return None
    pkg = spec.split("/")[-1].strip()
    if not pkg:
        return None
    return ["bash", "-lc", f"{shlex.quote(AUR_HELPER)} -S --noconfirm {shlex.quote(pkg)}"]


def fetch_packages_in_repo(repo_id, query="", installed_only=False, timeout=120, limit=3000):
    """List packages in a sync database using pacman -Sl / -Qi."""
    repo = (repo_id or "").strip()
    if not repo:
        return []
    q = (query or "").strip().lower()
    out = []
    try:
        if installed_only:
            r = subprocess.run(["pacman", "-Qi"], capture_output=True, text=True, timeout=timeout)
            for line in (r.stdout or "").splitlines():
                if line.startswith("Name") and q and q not in line.lower():
                    continue
            return out
        r = subprocess.run(["pacman", "-Sl", repo], capture_output=True, text=True, timeout=timeout)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            ver = parts[2] if len(parts) > 2 else "-"
            if q and q not in name.lower():
                continue
            installed = "[installed]" in line
            out.append({
                "name": name,
                "pkg": name,
                "version": ver,
                "repository": repo,
                "description": f"Package in {repo}",
                "installed": installed,
            })
            if len(out) >= limit:
                break
    except Exception as exc:
        log.debug("fetch_packages_in_repo(%s) failed: %s", repo, exc, exc_info=True)
    return out


class PackageOpWorker(threading.Thread):
    def __init__(self, command, operation, password, ui_callbacks):
        super().__init__(daemon=True)
        self.command = command
        self.operation = operation
        self.password = password
        self.ui = ui_callbacks  # dict: output(str), progress(int), status(str), done(success:bool, msg:str)

    def _progress_hint(self, line):
        l = line.lower()
        if 'downloading' in l:
            GLib.idle_add(self.ui['progress'], 50)
        elif 'installing' in l:
            GLib.idle_add(self.ui['progress'], 75)
        elif 'removing' in l:
            GLib.idle_add(self.ui['progress'], 60)
        elif 'loading' in l:
            GLib.idle_add(self.ui['progress'], 40)

    def run(self):
        GLib.idle_add(self.ui['status'], f"Starting {self.operation}...")
        GLib.idle_add(self.ui['progress'], 10)
        ret = run_command_stream(self.command, lambda t: GLib.idle_add(self.ui['output'], t), self._progress_hint, self.password)
        GLib.idle_add(self.ui['progress'], 100)
        if ret == 0:
            GLib.idle_add(self.ui['done'], True, f"{self.operation.capitalize()} completed successfully!")
        else:
            GLib.idle_add(self.ui['done'], False, f"{self.operation.capitalize()} failed with code {ret}")


class SearchWorker(threading.Thread):
    """Background search thread with a generation token for cancellation.

    When the caller bumps ``generation`` before starting a new worker the old
    thread's result is silently discarded — the UI callback is never called, so
    the package list is never replaced with stale data and the main loop is never
    blocked waiting for a slow query to finish.
    """

    def __init__(self, query, search_type, on_results, generation=0):
        super().__init__(daemon=True)
        self.query = query
        self.search_type = search_type
        self.on_results = on_results
        self.generation = generation
        self._cancelled = False

    def cancel(self):
        """Signal that this worker's result should be ignored."""
        self._cancelled = True

    def run(self):
        if self._cancelled:
            return
        try:
            st = self.search_type if self.search_type != "all" else "unified"
            pkgs = unified_search_packages(self.query, st)
        except Exception as exc:
            log.debug("SearchWorker failed: %s", exc, exc_info=True)
            pkgs = []
        # Only deliver results if not superseded by a newer search
        if not self._cancelled:
            GLib.idle_add(self.on_results, pkgs, self.generation)


# ----------------------------
# GTK UI
# ----------------------------

if GTK4_AVAILABLE and Gtk4 is not None:
    class PackageRow4(GObject4.Object):
        name = GObject4.Property(type=str, default="")
        version = GObject4.Property(type=str, default="-")
        repository = GObject4.Property(type=str, default="unknown")
        installed = GObject4.Property(type=str, default="No")
        description = GObject4.Property(type=str, default="")

        def __init__(self, name="", version="-", repository="unknown", installed=False, description="", raw_item=None):
            super().__init__()
            self.name = name
            self.version = version
            self.repository = repository
            self.installed = "Yes" if installed else "No"
            self.description = description
            self.raw_item = raw_item or {}

    class PkgerWindow(Adw.ApplicationWindow if Adw else Gtk4.ApplicationWindow):

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.set_title(f"{APP_NAME} — {DISTRO_INFO['name']}")
            self.set_default_size(800, 800)
            self.set_resizable(True)
            self._is_fullscreen4 = False

            self.settings = AppConfig.load()
            self.command_history = AppConfig.load_json(HISTORY_FILE, [])
            self.favorites = set(AppConfig.load_json(FAVORITES_FILE, []))
            self.system_stats = {}
            self.held_packages = []
            self.local_pkg_path = None

            self.packages_cache = []
            self.current_selection = None
            self.updates_data4 = []
            self.updates_data = []
            self.updates_summary4 = {}
            self._updates_load_generation = 0
            self.repo_data4 = {}
            self.selected_repo4 = None
            self.selected_repo_pkg4 = None
            self.selected_repo_pkg_names4 = set()
            self.command_history4 = self.command_history
            self.search_timer_id4 = 0
            self.search_generation = 0
            self._current_search_worker = None
            self.appimages_data4 = []
            self.sudo_password4 = None
            self.pkg_client_filter4 = ""
            self.pkg_installed_only4 = False
            self.repo_pkg_filter4 = ""
            self.repo_installed_only4 = False
            self.repo_pkg_results4 = []
            self.selected_repo_pkg_item4 = None
            self._repo_pkg_search_timer4 = 0
            self._repo_pkg_search_generation4 = 0
            self.system_log_buf4 = None
            self.pkg_sort_key4 = "name"
            self.pkg_sort_asc4 = True
            self._search_timer = None
            self._active_worker = None

            self._setup_ui()
            self._apply_theme()
            self._apply_css()
            self._setup_shortcuts()

           
            self.mode_stack4 = self.stack
            self.status_label = self.status_lbl
            self.progress = self.progress_bar

            self._load_initial_data()
            if hasattr(self, "_apply_install_pane_ratio4"):
                GLib.idle_add(self._apply_install_pane_ratio4)
                GLib.timeout_add(150, self._apply_install_pane_ratio4)
        def _apply_theme(self):
            """Follow system theme (light / dark / high-contrast)."""
            if Adw:
                mgr = Adw.StyleManager.get_default()
                mgr.set_color_scheme(Adw.ColorScheme.DEFAULT)
            try:
                settings = Gtk4.Settings.get_default()
                settings.connect("notify::gtk-theme-name", lambda *_: self._apply_css())
                settings.connect("notify::gtk-application-prefer-dark-theme", lambda *_: self._apply_css())
            except Exception:
                pass

        def _apply_css(self):
            css = """
            /* Surfaces — mix() works in all GTK4 themes (light / dark / HC) */
            .card {
                background-color: mix(@theme_bg_color, @theme_fg_color, 0.05);
                color: @theme_fg_color;
                border: 1px solid @borders;
                border-radius: 12px;
                padding: 14px;
            }
            .card:hover { border-color: alpha(@accent_color, 0.45); }

            .sidebar {
                background-color: mix(@window_bg_color, @theme_fg_color, 0.04);
                color: @theme_fg_color;
                border-right: 1px solid @borders;
                padding: 6px 0;
                min-width: 0;
            }
            .nav-wrap { min-width: 0; }
            .nav-section { font-size: 9px; font-weight: 700; letter-spacing: 1.2px;
                           color: alpha(@theme_fg_color, 0.45); margin: 8px 10px 3px 10px; }
            .nav-item { padding: 0; margin: 1px 6px; border-radius: 8px;
                        background: transparent; border: none; box-shadow: none;
                        min-height: 30px; }
            .nav-item:hover { background-color: alpha(@theme_fg_color, 0.08); }
            .nav-item.active {
                background-color: alpha(@accent_color, 0.15);
                color: @accent_color;
            }
            .nav-item.active .nav-label { font-weight: 700; color: @accent_color; }
            .nav-row   { padding: 5px 8px; }
            .nav-label { font-size: 12px; font-weight: 500; color: @theme_fg_color; }
            .nav-icon  { opacity: 0.85; min-width: 16px; color: @theme_fg_color; }
            .nav-item.active .nav-icon { opacity: 1; color: @accent_color; }

            .sidebar-brand { padding: 12px 10px 8px 10px; }
            .sidebar-brand-name { font-size: 15px; font-weight: 800; letter-spacing: 0.6px;
                                  color: @theme_fg_color; }
            .sidebar-brand-ver  { font-size: 10px; margin-top: 1px;
                                  color: alpha(@theme_fg_color, 0.55); }

            .status-bar {
                background-color: @window_bg_color;
                color: @theme_fg_color;
                border-top: 1px solid @borders;
                padding: 6px 14px;
                font-size: 12px;
            }

            .header-title { font-weight: 700; font-size: 13px; color: @theme_fg_color; margin-left: 4px; }
            .header-menu-btn { border-radius: 50%; padding: 6px; }

            /* Typography */
            .section-title { font-weight: 800; font-size: 15px; color: @theme_fg_color; }
            .dim-label     { color: alpha(@theme_fg_color, 0.55); font-size: 13px; }

            /* Dashboard */
            .dash-card        { padding: 10px 12px; border-radius: 10px; min-width: 108px; }
            .dash-card-title  { font-size: 10px; font-weight: 600; letter-spacing: 0.4px;
                                color: alpha(@theme_fg_color, 0.55); text-transform: uppercase; }
            .dash-card-value  { font-size: 15px; font-weight: 700; color: @theme_fg_color; margin-top: 2px; }
            .dash-health-card { padding: 12px 14px; border-radius: 12px; min-width: 120px; }
            .dash-health-value{ font-size: 26px; font-weight: 800; line-height: 1; }
            .dash-welcome     { font-size: 20px; font-weight: 700; color: @theme_fg_color; }
            .dash-subtitle    { font-size: 12px; }
            .dash-actions button { padding: 6px 14px; font-size: 12px; border-radius: 20px; }
            .dash-stats-line  { font-size: 11px; padding: 8px 12px; border-radius: 8px;
                                background: alpha(@theme_fg_color, 0.05);
                                color: alpha(@theme_fg_color, 0.65); }

            /* Semantic colors — adapt to every theme automatically */
            .health-excellent { color: @success_color;  font-weight: 800; }
            .health-good      { color: @accent_color;   font-weight: 800; }
            .health-warning   { color: @warning_color;  font-weight: 800; }
            .health-critical  { color: @error_color;    font-weight: 800; }

            .badge-security {
                background: alpha(@error_color, 0.15); color: @error_color;
                border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 700;
            }
            .badge-kernel {
                background: alpha(@warning_color, 0.15); color: @warning_color;
                border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 700;
            }
            .badge-normal {
                background: alpha(@theme_fg_color, 0.08); color: @theme_fg_color;
                border-radius: 6px; padding: 2px 8px; font-size: 11px;
            }

            /* DEB installer */
            .pkg-summary-ok    { color: @success_color;  font-weight: 700; font-size: 13px; }
            .pkg-summary-warn  { color: @warning_color;  font-weight: 700; font-size: 13px; }
            .pkg-summary-error { color: @error_color;    font-weight: 700; font-size: 13px; }
            .pkg-meta          { font-size: 11px; color: alpha(@theme_fg_color, 0.55); }
            .pkg-meta-val      { font-size: 12px; font-weight: 600; color: @theme_fg_color; }
            .pkg-dep-ok        { color: @success_color; font-size: 11px; font-weight: 600; }
            .pkg-dep-miss      { color: @error_color;   font-size: 11px; font-weight: 600; }
            .pkg-drop-hint {
                font-size: 12px; color: alpha(@theme_fg_color, 0.55);
                padding: 20px; border: 1px dashed @borders; border-radius: 10px;
            }

            /* Terminal / logs — uses view colors from theme */
            .terminal-view {
                font-family: monospace; font-size: 13px;
                background-color: @view_bg_color;
                color: @view_fg_color;
                padding: 12px; border-radius: 8px;
                border: 1px solid @borders;
            }

            .search-spinner { opacity: 0.7; }
            """
            if getattr(self, "_css_provider", None):
                try:
                    Gtk4.StyleContext.remove_provider_for_display(
                        Gdk4.Display.get_default(), self._css_provider,
                    )
                except Exception:
                    pass
            self._css_provider = Gtk4.CssProvider()
            self._css_provider.load_from_data(css.encode("utf-8"))
            Gtk4.StyleContext.add_provider_for_display(
                Gdk4.Display.get_default(),
                self._css_provider,
                Gtk4.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        def _setup_ui(self):
            body = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL)
            body.set_vexpand(True)

            self.sidebar_revealer = Gtk4.Revealer()
            self.sidebar_revealer.set_transition_type(Gtk4.RevealerTransitionType.SLIDE_RIGHT)
            self.sidebar_revealer.set_reveal_child(True)
            self.sidebar_revealer.set_hexpand(False)
            self.sidebar_revealer.set_halign(Gtk4.Align.START)

            self.sidebar = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=0)
            self.sidebar.set_hexpand(False)
            self.sidebar.set_halign(Gtk4.Align.START)
            self.sidebar.add_css_class("sidebar")
            self.sidebar_revealer.set_child(self.sidebar)
            body.append(self.sidebar_revealer)

            brand = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=0)
            brand.add_css_class("sidebar-brand")
            brand.set_halign(Gtk4.Align.START)
            brand_name = Gtk4.Label(label=APP_NAME)
            brand_name.add_css_class("sidebar-brand-name")
            brand_name.set_xalign(0)
            brand_ver = Gtk4.Label(label=f"v{APP_VERSION}")
            brand_ver.add_css_class("sidebar-brand-ver")
            brand_ver.set_xalign(0)
            brand.append(brand_name)
            brand.append(brand_ver)
            self.sidebar.append(brand)

            self.nav_btns = {}
            nav_sections = [
                ("MAIN", [
                    ("dash", "Home", "computer-symbolic"),
                ]),
                ("PACKAGES", [
                    ("install", "Install", "system-software-install-symbolic"),
                    ("localpkg", "Local PKG", "package-x-generic-symbolic"),
                    ("updates", "Updates", "software-update-available-symbolic"),
                ]),
                ("SYSTEM", [
                    ("repos", "Sources", "network-server-symbolic"),
                    ("appimages", "Images", "package-x-generic-symbolic"),
                    ("system", "Tools", "system-run-symbolic"),
                    ("settings", "Settings", "preferences-system-symbolic"),
                ]),
                ("MORE", [
                    ("logs", "Logs", "utilities-terminal-symbolic"),
                    ("news", "News", "dialog-information-symbolic"),
                ]),
            ]

            scroll_nav = Gtk4.ScrolledWindow()
            scroll_nav.set_policy(Gtk4.PolicyType.NEVER, Gtk4.PolicyType.AUTOMATIC)
            scroll_nav.set_vexpand(True)
            scroll_nav.set_hexpand(False)
            scroll_nav.set_halign(Gtk4.Align.START)
            nav_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=0)
            nav_box.add_css_class("nav-wrap")
            nav_box.set_halign(Gtk4.Align.START)
            nav_box.set_hexpand(False)
            scroll_nav.set_child(nav_box)
            self.sidebar.append(scroll_nav)

            for section_title, items in nav_sections:
                self._add_nav_section(nav_box, section_title)
                for name, label, icon in items:
                    nav_box.append(self._create_nav_item(name, label, icon))

            self.content_area = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL)
            self.content_area.set_hexpand(True)
            body.append(self.content_area)

            self.toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=10)
            self.toolbar.set_margin_top(6)
            self.toolbar.set_margin_bottom(6)
            self.toolbar.set_margin_start(14)
            self.toolbar.set_margin_end(14)

            self.page_title_lbl = Gtk4.Label(label="Home")
            self.page_title_lbl.add_css_class("section-title")
            self.page_title_lbl.set_hexpand(True)
            self.page_title_lbl.set_xalign(0)
            self.toolbar.append(self.page_title_lbl)
            self.content_area.append(self.toolbar)

            self.stack = Gtk4.Stack()
            self.stack.set_transition_type(Gtk4.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.stack.set_vexpand(True)
            self.content_area.append(self.stack)
            self._init_pages()

            self.bottom_bar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=12)
            self.bottom_bar.add_css_class("status-bar")
            self.status_lbl = Gtk4.Label(label="Ready")
            self.status_lbl.set_xalign(0)
            self.bottom_bar.append(self.status_lbl)
            self.progress_bar = Gtk4.ProgressBar()
            self.progress_bar.set_hexpand(True)
            self.progress_bar.set_show_text(True)
            self.bottom_bar.append(self.progress_bar)
            self.content_area.append(self.bottom_bar)

            header = Adw.HeaderBar() if Adw else Gtk4.HeaderBar()
            if Adw:
                header.set_show_start_title_buttons(True)
                header.set_show_end_title_buttons(True)
            else:
                header.set_show_title_buttons(True)
            self._build_header_menu(header)

            if Adw:
                toolbar_view = Adw.ToolbarView()
                toolbar_view.add_top_bar(header)
                toolbar_view.set_content(body)
                self.toast_overlay = Adw.ToastOverlay()
                self.toast_overlay.set_child(toolbar_view)
                self.set_content(self.toast_overlay)
            else:
                self.set_titlebar(header)
                self.set_child(body)
                self.toast_overlay = None

            self._switch_page("dash")

        def _add_nav_section(self, parent, title):
            lbl = Gtk4.Label(label=title)
            lbl.add_css_class("nav-section")
            lbl.set_xalign(0)
            lbl.set_halign(Gtk4.Align.START)
            lbl.set_hexpand(False)
            parent.append(lbl)

        def _create_nav_item(self, name, label, icon):
            row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("nav-row")
            row.set_halign(Gtk4.Align.START)
            row.set_hexpand(False)
            icon_img = Gtk4.Image.new_from_icon_name(icon)
            icon_img.set_pixel_size(15)
            icon_img.add_css_class("nav-icon")
            text = Gtk4.Label(label=label)
            text.add_css_class("nav-label")
            text.set_xalign(0)
            text.set_hexpand(False)
            row.append(icon_img)
            row.append(text)

            btn = Gtk4.Button()
            btn.add_css_class("nav-item")
            btn.set_child(row)
            btn.set_halign(Gtk4.Align.START)
            btn.set_hexpand(False)
            btn.set_tooltip_text(label)
            btn.connect("clicked", lambda _x, n=name: self._switch_page(n))
            self.nav_btns[name] = btn
            return btn

        def _build_header_menu(self, header):
            title = Gtk4.Label(label=APP_NAME)
            title.add_css_class("header-title")
            header.pack_start(title)

            search_btn = Gtk4.Button(icon_name="system-search-symbolic")
            search_btn.set_tooltip_text("Search (Ctrl+F)")
            search_btn.connect("clicked", lambda _x: self._focus_search())
            header.pack_end(search_btn)

            refresh_btn = Gtk4.Button(icon_name="view-refresh-symbolic")
            refresh_btn.set_tooltip_text("Refresh (Ctrl+R)")
            refresh_btn.connect("clicked", lambda _x: self._refresh_current_page())
            header.pack_end(refresh_btn)

            menu_btn = Gtk4.MenuButton()
            menu_btn.add_css_class("header-menu-btn")
            menu_btn.set_icon_name("open-menu-symbolic")
            menu_btn.set_tooltip_text("Menu")

            menu = Gio4.Menu()

            maint = Gio4.Menu()
            maint.append("Upgrade All", "win.upgrade")
            maint.append("Clean Cache", "win.clean")
            maint.append("Fix Dependencies", "win.fix")
            menu.append_submenu("Maintenance", maint)

            hist = Gio4.Menu()
            hist.append("Command History", "win.history")
            hist.append("Pacman Log", "win.pacman-log")
            menu.append_submenu("History", hist)

            view = Gio4.Menu()
            view.append("Toggle Sidebar", "win.toggle-sidebar")
            view.append("Fullscreen", "win.fullscreen")
            menu.append_submenu("View", view)

            app_menu = Gio4.Menu()
            app_menu.append("About", "win.about")
            app_menu.append("Quit", "win.quit")
            menu.append_section(None, app_menu)

            menu_btn.set_menu_model(menu)
            header.pack_end(menu_btn)

            self._add_action("about", self._show_about)
            self._add_action("history", self._show_history)
            self._add_action("pacman-log", self._show_pacman_log)
            self._add_action("fullscreen", self._toggle_fullscreen)
            self._add_action("toggle-sidebar", self._toggle_sidebar)
            self._add_action("upgrade", lambda: self._quick_action("upgrade"))
            self._add_action("clean", lambda: self._quick_action("clean"))
            self._add_action("fix", lambda: self._quick_action("fix"))
            self._add_action("quit", self._quit_app)

        def _toggle_sidebar(self):
            self.sidebar_revealer.set_reveal_child(not self.sidebar_revealer.get_reveal_child())

        def _quit_app(self):
            app = self.get_application()
            if app:
                app.quit()
            else:
                self.close()

        def _ensure_win_action_group(self):
            if getattr(self, "_win_action_group", None) is not None:
                return
            self._win_action_group = Gio4.SimpleActionGroup()
            self.insert_action_group("win", self._win_action_group)

        def _add_action(self, name, callback):
            self._ensure_win_action_group()
            action = Gio4.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p: callback())
            self._win_action_group.add_action(action)

        def _setup_shortcuts(self):
            ctrl = Gdk4.ModifierType.CONTROL_MASK
            self.add_shortcut(Gtk4.Shortcut.new(
                Gtk4.ShortcutTrigger.parse_string("<Primary>f"),
                Gtk4.CallbackAction.new(lambda *_: self._focus_search() or True),
            ))
            self.add_shortcut(Gtk4.Shortcut.new(
                Gtk4.ShortcutTrigger.parse_string("<Primary>r"),
                Gtk4.CallbackAction.new(lambda *_: self._refresh_current_page() or True),
            ))
            self.add_shortcut(Gtk4.Shortcut.new(
                Gtk4.ShortcutTrigger.parse_string("F11"),
                Gtk4.CallbackAction.new(lambda *_: self._toggle_fullscreen() or True),
            ))

        def _focus_search(self):
            self._switch_page("install")
            self.search_entry4.grab_focus() if hasattr(self, 'search_entry4') else None

        def _init_pages(self):
            self.stack.add_named(self._create_dash_page(), "dash")
            self.stack.add_named(self._create_install_workspace4(), "install")
            self.stack.add_named(self._create_local_pkg_page(), "localpkg")
            self.stack.add_named(self._create_updates_workspace4(), "updates")
            self.stack.add_named(self._create_repos_page_gtk4(), "repos")
            self.stack.add_named(self._create_appimages_page4(), "appimages")
            self.stack.add_named(self._create_system_workspace(), "system")
            self.stack.add_named(self._create_settings_page(), "settings")
            self.stack.add_named(self._create_logs_page(), "logs")
            self.stack.add_named(self._create_news_page(), "news")


        def _create_local_pkg_page(self):
            scroll = Gtk4.ScrolledWindow()
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=12)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(14)
            box.set_margin_end(14)
            scroll.set_child(box)

            title = Gtk4.Label(label="Install Local Arch Package")
            title.add_css_class("section-title")
            title.set_xalign(0)
            box.append(title)

            hint = Gtk4.Label(
                label="Select a .pkg.tar.zst or .pkg.tar.xz file to install with pacman -U.",
            )
            hint.set_xalign(0)
            hint.set_wrap(True)
            hint.add_css_class("dim-label")
            box.append(hint)

            toolbar = Gtk4.Box(spacing=8)
            open_btn = Gtk4.Button(label="Open package…")
            open_btn.set_icon_name("document-open-symbolic")
            open_btn.add_css_class("suggested-action")
            open_btn.connect("clicked", lambda *_: self._install_local_package4())
            toolbar.append(open_btn)

            self.local_pkg_path_entry = Gtk4.Entry(placeholder_text="No package selected")
            self.local_pkg_path_entry.set_hexpand(True)
            self.local_pkg_path_entry.set_editable(False)
            toolbar.append(self.local_pkg_path_entry)
            box.append(toolbar)

            actions = Gtk4.Box(spacing=10)
            install_btn = Gtk4.Button(label="Install Package")
            install_btn.add_css_class("suggested-action")
            install_btn.connect("clicked", lambda *_: self._install_local_package4())
            actions.append(install_btn)
            box.append(actions)

            note = Gtk4.Label(label="Tip: use pacman -U for local packages built from AUR or official repos.")
            note.add_css_class("pkg-drop-hint")
            note.set_xalign(0)
            note.set_wrap(True)
            box.append(note)
            return scroll


        def _create_dash_page(self):
            scroll = Gtk4.ScrolledWindow()
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=14)
            box.set_margin_top(18)
            box.set_margin_bottom(18)
            box.set_margin_start(24)
            box.set_margin_end(24)
            scroll.set_child(box)

            head_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=4)
            welcome = Gtk4.Label(label=f"Welcome to {APP_NAME}")
            welcome.add_css_class("dash-welcome")
            welcome.set_xalign(0)
            head_box.append(welcome)

            sub = Gtk4.Label(
                label=f"{DISTRO_INFO['name']} {DISTRO_INFO['version']} — Intelligent Package Management",
            )
            sub.add_css_class("dash-subtitle")
            sub.add_css_class("dim-label")
            sub.set_xalign(0)
            head_box.append(sub)
            box.append(head_box)

            cards_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=10)
            cards_row.set_homogeneous(False)

            health_card = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=4)
            health_card.add_css_class("card")
            health_card.add_css_class("dash-health-card")
            hl = Gtk4.Label(label="Health")
            hl.add_css_class("dash-card-title")
            hl.set_xalign(0)
            self.dash_health_lbl = Gtk4.Label(label="—")
            self.dash_health_lbl.add_css_class("dash-health-value")
            self.dash_health_lbl.add_css_class("health-good")
            self.dash_health_lbl.set_xalign(0)
            health_card.append(hl)
            health_card.append(self.dash_health_lbl)
            cards_row.append(health_card)

            self.dash_grid = Gtk4.Grid()
            self.dash_grid.set_column_spacing(8)
            self.dash_grid.set_row_spacing(8)
            cards_row.append(self.dash_grid)
            cards_row.set_hexpand(True)
            box.append(cards_row)

            def add_card(title, value, row, col):
                c = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=2)
                c.add_css_class("card")
                c.add_css_class("dash-card")
                tl = Gtk4.Label(label=title)
                tl.add_css_class("dash-card-title")
                tl.set_xalign(0)
                vl = Gtk4.Label(label=value)
                vl.add_css_class("dash-card-value")
                vl.set_xalign(0)
                c.append(tl)
                c.append(vl)
                self.dash_grid.attach(c, col, row, 1, 1)
                return vl

            self.dash_os_lbl = add_card("OS", DISTRO_INFO["name"], 0, 0)
            self.dash_pkgs_lbl = add_card("Installed", "...", 0, 1)
            self.dash_upd_lbl = add_card("Updates", "...", 0, 2)
            self.dash_sec_lbl = add_card("Security", "...", 1, 0)
            self.dash_cache_lbl = add_card("Cache", "...", 1, 1)
            self.dash_kernel_lbl = add_card("Kernel", subprocess.getoutput("uname -r"), 1, 2)

            actions = Gtk4.Box(spacing=8)
            actions.add_css_class("dash-actions")
            for label, page in [("Updates", "updates"), ("Search", "install"), ("Local PKG", "localpkg"), ("Sources", "repos")]:
                b = Gtk4.Button(label=label)
                if page == "updates":
                    b.add_css_class("suggested-action")
                b.connect("clicked", lambda _x, p=page: self._switch_page(p))
                actions.append(b)
            box.append(actions)

            self.dash_stats_lbl = Gtk4.Label(label="")
            self.dash_stats_lbl.add_css_class("dash-stats-line")
            self.dash_stats_lbl.set_xalign(0)
            box.append(self.dash_stats_lbl)
            return scroll

        def _create_settings_page(self):
            scroll = Gtk4.ScrolledWindow()
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=16)
            box.set_margin_top(20)
            box.set_margin_bottom(20)
            box.set_margin_start(24)
            box.set_margin_end(24)
            scroll.set_child(box)

            title = Gtk4.Label(label="Settings")
            title.add_css_class("title-2")
            title.set_xalign(0)
            box.append(title)

            self.setting_auto_check = Gtk4.CheckButton(label="Auto-check updates on startup")
            self.setting_auto_check.set_active(self.settings.get("auto_check_updates", True))
            box.append(self.setting_auto_check)

            self.setting_security = Gtk4.CheckButton(label="Show security alerts prominently")
            self.setting_security.set_active(self.settings.get("show_security_alerts", True))
            box.append(self.setting_security)

            self.setting_confirm_install = Gtk4.CheckButton(label="Confirm before installing packages")
            self.setting_confirm_install.set_active(self.settings.get("confirm_install", True))
            box.append(self.setting_confirm_install)

            self.setting_confirm_remove = Gtk4.CheckButton(label="Confirm before removing packages")
            self.setting_confirm_remove.set_active(self.settings.get("confirm_remove", True))
            box.append(self.setting_confirm_remove)

            src_lbl = Gtk4.Label(label="Search Sources", xalign=0)
            src_lbl.add_css_class("section-title")
            box.append(src_lbl)
            self.setting_search_official = Gtk4.CheckButton(label="Include official (pacman)")
            self.setting_search_official.set_active(self.settings.get("search_official", True))
            box.append(self.setting_search_official)
            self.setting_search_aur = Gtk4.CheckButton(label="Include AUR")
            self.setting_search_aur.set_active(self.settings.get("search_aur", True))
            box.append(self.setting_search_aur)
            self.setting_search_flatpak = Gtk4.CheckButton(label="Include Flatpak")
            self.setting_search_appimage = Gtk4.CheckButton(label="Include AppImage")
            self.setting_search_appimage.set_active(self.settings.get("search_appimage", True))
            box.append(self.setting_search_appimage)
            self.setting_search_flatpak.set_active(self.settings.get("search_flatpak", True))
            box.append(self.setting_search_flatpak)

            save_btn = Gtk4.Button(label="Save Settings")
            save_btn.add_css_class("suggested-action")
            save_btn.connect("clicked", self._save_settings)
            box.append(save_btn)
            return scroll

        def _create_logs_page(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=5)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(10)
            box.set_margin_end(10)

            self.log_buffer = Gtk4.TextBuffer()
            self.log_view = Gtk4.TextView(buffer=self.log_buffer)
            self.log_view.set_editable(False)
            self.log_view.set_monospace(True)
            self.log_view.add_css_class("terminal-view")

            scroll = Gtk4.ScrolledWindow()
            scroll.set_child(self.log_view)
            scroll.set_vexpand(True)
            box.append(scroll)

            btn_row = Gtk4.Box(spacing=10)
            clear_btn = Gtk4.Button(label="Clear")
            clear_btn.connect("clicked", lambda _x: self.log_buffer.set_text(""))
            btn_row.append(clear_btn)
            export_btn = Gtk4.Button(label="Export Log")
            export_btn.connect("clicked", self._export_log)
            btn_row.append(export_btn)
            box.append(btn_row)
            return box

        def _create_news_page(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=10)
            box.set_margin_top(15)
            box.set_margin_bottom(15)
            box.set_margin_start(15)
            box.set_margin_end(15)

            toolbar = Gtk4.Box(spacing=10)
            lbl = Gtk4.Label(label=f"News — {DISTRO_INFO['name']}")
            lbl.add_css_class("section-title")
            toolbar.append(lbl)
            refresh = Gtk4.Button(label="Refresh")
            refresh.connect("clicked", lambda _x: self._load_news())
            toolbar.append(refresh)
            box.append(toolbar)

            self.news_buffer = Gtk4.TextBuffer()
            self.news_view = Gtk4.TextView(buffer=self.news_buffer)
            self.news_view.set_editable(False)
            self.news_view.set_wrap_mode(Gtk4.WrapMode.WORD)

            scroll = Gtk4.ScrolledWindow()
            scroll.set_child(self.news_view)
            scroll.set_vexpand(True)
            box.append(scroll)
            return box

        # --- Navigation & State ---

        def _switch_page(self, name):
            titles = {
                "dash": "Home", "install": "Install", "localpkg": "Local Package",
                "updates": "Updates",
                "repos": "Sources", "appimages": "AppImages", "system": "Tools",
                "settings": "Settings", "logs": "Logs", "news": "News",
            }
            self.stack.set_visible_child_name(name)
            self.page_title_lbl.set_label(titles.get(name, name.capitalize()))
            for k, v in self.nav_btns.items():
                if k == name:
                    v.add_css_class("active")
                else:
                    v.remove_css_class("active")
            if name == "repos":
                self._load_repos()

        def _refresh_current_page(self):
            page = self.stack.get_visible_child_name()
            if page == "updates":
                self._load_updates()
            elif page == "repos":
                self._load_repos()
            elif page == "appimages":
                self._load_appimages4()
            elif page == "news":
                self._load_news()
            elif page == "dash":
                self.system_stats = get_system_stats()
                self._update_dash()
            else:
                self._gtk4_refresh_all_data4()

        def _toast(self, message, timeout=3):
            if Adw and self.toast_overlay:
                toast = Adw.Toast.new(message)
                toast.set_timeout(timeout)
                self.toast_overlay.add_toast(toast)

        def _record_history(self, label, cmd):
            entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "label": label, "cmd": cmd}
            self.command_history.append(entry)
            if len(self.command_history) > 200:
                self.command_history = self.command_history[-200:]
            AppConfig.save_json(HISTORY_FILE, self.command_history)


        def _load_news(self):
            self._set_status("Loading news…")

            def worker():
                items = fetch_arch_news_items(limit=25)
                lines = []
                for it in items:
                    lines.append(it.get("title", "-"))
                    if it.get("pub_date"):
                        lines.append(f"  {it['pub_date']}")
                    if it.get("link"):
                        lines.append(f"  {it['link']}")
                    if it.get("summary"):
                        summary = re.sub(r"<[^>]+>", "", it["summary"])[:400]
                        lines.append(f"  {summary}")
                    lines.append("")
                text = "\n".join(lines) if lines else "No news items available."
                GLib.idle_add(self.news_buffer.set_text, text)
                GLib.idle_add(self._set_status, "News loaded")

            threading.Thread(target=worker, daemon=True).start()


        def _load_arch_news4(self):
            self._load_news()


        def _run_privileged_command(self, cmd, label=None):
            label = label or cmd
            self._switch_page("logs")
            self._record_history(label, cmd)
            if hasattr(self, "log_buffer"):
                GLib.idle_add(self.log_buffer.set_text, f"$ {cmd}\n{'=' * 50}\n")
            self._run_stream_command(["bash", "-lc", cmd], label, requires_privilege=True)

        # --- Data Loading ---


        def _load_initial_data(self):
            self._set_status("Initializing…")
            self._set_progress(10, "Loading")

            def worker():
                self._set_progress(20, "Packages")
                self._load_installed_packages_sync()
                self._set_progress(40, "System")
                self.system_stats = get_system_stats()
                self.held_packages = []
                GLib.idle_add(self._update_dash)
                self._set_progress(60, "Updates")
                if self.settings.get("auto_check_updates", True):
                    self._load_updates()
                self._set_progress(70, "Repos")
                self._load_repos_sync()
                self._set_progress(80, "AppImages")
                self._load_appimages_sync()
                self._load_news()
                self._set_progress(100, "")
                self._set_status("Ready")
                sec_count = sum(
                    1 for u in self.updates_data4
                    if classify_update_severity(
                        u.get("name"), u.get("repository"), u.get("source"),
                        u.get("severity"), u.get("kind"),
                    ) == "security"
                )
                if sec_count and self.settings.get("show_security_alerts", True):
                    GLib.idle_add(lambda: self._toast(f"⚠ {sec_count} security update(s) available!", 5))

            threading.Thread(target=worker, daemon=True).start()

        def _load_installed_packages_sync(self):
            try:
                r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=60)
                self.packages_cache = []
                for line in (r.stdout or "").splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        self.packages_cache.append({
                            "name": parts[0],
                            "pkg": parts[0],
                            "version": parts[1],
                            "repository": "installed",
                            "description": "Installed package",
                            "installed": True,
                            "source": "pacman",
                        })
            except Exception:
                self.packages_cache = []

        def _load_repos_sync(self):
            try:
                data = {}
                r = subprocess.run(["pacman", "-Sl"], capture_output=True, text=True, timeout=70)
                for line in (r.stdout or "").splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        repo, name = parts[0], parts[1]
                        installed = "[installed]" in line
                        data.setdefault(repo, []).append({"name": name, "installed": installed})
                self.repo_data4 = data
            except Exception:
                self.repo_data4 = {}

        def _load_appimages_sync(self):
            try:
                self.appimages_data4 = scan_appimages()
            except Exception:
                self.appimages_data4 = []

        def _update_dash(self):
            updates = self.updates_data4 or self.updates_data or []
            score = compute_health_score(
                len(self.packages_cache), updates, self.held_packages, self.system_stats,
            )
            if hasattr(self, "dash_health_lbl"):
                self.dash_health_lbl.set_label(f"{score}%")
                for cls in ("health-excellent", "health-good", "health-warning", "health-critical"):
                    self.dash_health_lbl.remove_css_class(cls)
                if score >= 90:
                    self.dash_health_lbl.add_css_class("health-excellent")
                elif score >= 70:
                    self.dash_health_lbl.add_css_class("health-good")
                elif score >= 50:
                    self.dash_health_lbl.add_css_class("health-warning")
                else:
                    self.dash_health_lbl.add_css_class("health-critical")
            if hasattr(self, "dash_pkgs_lbl"):
                self.dash_pkgs_lbl.set_label(str(len(self.packages_cache)))
            if hasattr(self, "dash_upd_lbl"):
                self.dash_upd_lbl.set_label(str(len(updates)))
            if hasattr(self, "dash_sec_lbl"):
                sec = sum(
                    1 for u in updates
                    if classify_update_severity(
                        u.get("name"), u.get("repository"), u.get("source"),
                        u.get("severity"), u.get("kind"),
                    ) == "security"
                )
                self.dash_sec_lbl.set_label(str(sec))
            if hasattr(self, "dash_cache_lbl"):
                cache = self.system_stats.get("cache_size", 0)
                self.dash_cache_lbl.set_label(human_size(cache))
            if hasattr(self, "dash_stats_lbl"):
                stats = self.system_stats or {}
                self.dash_stats_lbl.set_label(
                    f"Uptime: {stats.get('uptime', '-')}  ·  Load: {stats.get('load', '-')}  ·  "
                    f"Disk free: {stats.get('disk_free', '-')}  ·  RAM used: {stats.get('mem_used_pct', '-')}",
                )
            self._refresh_dashboard()

        def _clear_list(self, listbox):
            c = listbox.get_first_child()
            while c:
                listbox.remove(c)
                c = listbox.get_first_child()

        # --- Search & Install ---


        def _save_settings(self):
            self.settings = {
                "auto_check_updates": self.setting_auto_check.get_active(),
                "show_security_alerts": self.setting_security.get_active(),
                "confirm_install": self.setting_confirm_install.get_active(),
                "confirm_remove": self.setting_confirm_remove.get_active(),
                "search_official": self.setting_search_official.get_active(),
                "search_aur": self.setting_search_aur.get_active(),
                "search_appimage": self.setting_search_appimage.get_active(),
                "search_flatpak": self.setting_search_flatpak.get_active(),
            }
            AppConfig.save(self.settings)
            self._toast("Settings saved")


        def _export_log(self, _b):
            dialog = Gtk4.FileDialog(title="Export Log")
            dialog.set_initial_name(f"pkger-log-{datetime.now():%Y%m%d-%H%M%S}.txt")

            def on_save(_d, result):
                try:
                    file = dialog.save_finish(result)
                    start, end = self.log_buffer.get_bounds()
                    text = self.log_buffer.get_text(start, end, False)
                    with open(file.get_path(), "w") as f:
                        f.write(text)
                    self._toast("Log exported")
                except Exception:
                    pass

            dialog.save(self, None, on_save)

        def _show_about(self):
            dialog = Gtk4.AboutDialog()
            dialog.set_transient_for(self)
            dialog.set_program_name(APP_NAME)
            dialog.set_version(APP_VERSION)
            dialog.set_authors([APP_DEVELOPER])
            dialog.set_copyright(f"© {APP_YEAR} {APP_DEVELOPER}")
            dialog.set_comments(
                "Professional Package Manager for Arch Linux and derivatives.\n"
                "pacman · AUR · Flatpak · AppImage · Security Intelligence",
            )
            dialog.set_license_type(Gtk4.License.GPL_3_0)
            dialog.set_website("https://github.com/almezali/pkger-g")
            dialog.set_logo_icon_name("system-software-install-symbolic")
            dialog.present()

        def _show_history(self):
            dialog = Gtk4.Dialog(title="Command History", transient_for=self, modal=True)
            dialog.set_default_size(640, 420)
            area = dialog.get_content_area()
            scroll = Gtk4.ScrolledWindow()
            scroll.set_vexpand(True)
            lst = Gtk4.ListBox()
            for h in reversed(self.command_history[-50:]):
                row = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=4)
                row.set_margin_top(8)
                row.set_margin_bottom(8)
                row.set_margin_start(12)
                row.set_margin_end(12)
                row.append(Gtk4.Label(label=f"[{h['time']}] {h['label']}", xalign=0, css_classes=["bold"]))
                row.append(Gtk4.Label(label=h.get("cmd", ""), xalign=0, css_classes=["dim-label"]))
                lst.append(row)
            scroll.set_child(lst)
            area.append(scroll)
            dialog.add_button("Close", Gtk4.ResponseType.CLOSE)
            dialog.present()

        def _toggle_fullscreen(self):
            if self.is_fullscreen():
                self.unfullscreen()
                self._is_fullscreen4 = False
            else:
                self.fullscreen()
                self._is_fullscreen4 = True

        def _quick_action(self, action):
            mapping = {
                "upgrade": "update",
                "clean": "clean",
                "fix": "fixdeps",
            }
            key = mapping.get(action, action)
            self._run_system_action(key)


        def _show_pacman_log(self):
            self._switch_page("logs")
            path = "/var/log/pacman.log"
            def worker():
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    tail = "".join(lines[-500:])
                    GLib.idle_add(self.log_buffer.set_text, tail)
                except Exception as exc:
                    GLib.idle_add(self.log_buffer.set_text, f"Cannot read {path}: {exc}")
            threading.Thread(target=worker, daemon=True).start()


        def _append_output(self, text):
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {text}\n"

            def apply():
                if hasattr(self, "output_buf4") and self.output_buf4:
                    end = self.output_buf4.get_end_iter()
                    self.output_buf4.insert(end, line)
                if hasattr(self, "log_buffer") and self.log_buffer:
                    self.log_buffer.insert(self.log_buffer.get_end_iter(), line)
                buf = getattr(self, "system_log_buf4", None)
                if buf is not None:
                    e2 = buf.get_end_iter()
                    buf.insert(e2, line)
            GLib.idle_add(apply)
        def _appimage_path4(self, item):
            if not item:
                return ""
            path = item.get("path")
            if path:
                return path
            desc = item.get("description", "")
            if desc.startswith("Local AppImage:"):
                return desc.split(":", 1)[1].strip()
            return ""
        def _apply_appimages_data4(self, data):
            self.appimages_data4 = list(data or [])
            self._filter_appimages4()
            self._set_status(f"Found {len(self.appimages_data4)} AppImages")
        def _apply_install_pane_ratio4(self):
            """Install layout tuned for 800×800: table on top, details below."""
            main = getattr(self, "install_main_paned4", None)
            if main is not None:
                h = main.get_allocated_height()
                if h < 1:
                    h = max(400, self.get_allocated_height() - 200)
                if h > 200:
                    main.set_position(max(180, int(h * 0.58)))

            right = getattr(self, "install_right_paned4", None)
            if right is not None:
                w = right.get_allocated_width()
                if w < 1:
                    w = max(360, self.get_allocated_width() - 180)
                if w > 120:
                    right.set_position(max(120, int(w * 0.5)))
        def _install_search_spinner4(self, running):
            sp = getattr(self, "install_search_spinner4", None)
            if not sp:
                return
            if running:
                sp.start()
            else:
                sp.stop()

        def _quick_install_aur4(self):
            spec = (self.aur_quick_entry4.get_text() or "").strip()
            if not spec:
                self._toast("Enter AUR package name")
                return
            cmd = build_aur_install_command(spec)
            if not cmd:
                self._toast("Invalid package name or AUR helper missing")
                return
            self._run_stream_command(cmd, f"Install AUR {spec}", requires_privilege=False)
            GLib.timeout_add(2500, lambda: (self._load_installed_packages(), False)[1])

        def _quick_add_repo_url4(self):
            url = (self.repo_url_quick_entry4.get_text() or "").strip()
            if not url:
                self._toast("Enter a mirrorlist URL or local path")
                return
            cmd = build_addrepo_from_url_command(url)
            if not cmd:
                self._toast("Invalid repository URL")
                return
            self._run_stream_command(cmd, "Add repository", requires_privilege=True)
            GLib.timeout_add(2500, lambda: (self._load_repos(), False)[1])

        def _apply_pkg_sort_toggle4(self, column_key):
            if self.pkg_sort_key4 == column_key:
                self.pkg_sort_asc4 = not self.pkg_sort_asc4
            else:
                self.pkg_sort_key4 = column_key
                self.pkg_sort_asc4 = True
            self._sort_packages_cache4()
            self._refresh_pkg_list_view4()
        def _apply_selected_updates(self):
            selected = [i for i in self.updates_data4 if i.get("selected")]
            if not selected:
                self._append_output("No updates selected.")
                return
            pacman_pkgs = [i["name"] for i in selected if i.get("source", "pacman") == "pacman"]
            flatpak_apps = [i["name"] for i in selected if i.get("source") == "flatpak"]
            if pacman_pkgs:
                cmd = ["bash", "-lc", f"pacman -S --noconfirm {' '.join(shlex.quote(n) for n in pacman_pkgs)}"]
                self._run_stream_command(cmd, "apply selected pacman updates", requires_privilege=True)
            if flatpak_apps:
                cmd = ["bash", "-lc", f"flatpak update -y {' '.join(shlex.quote(n) for n in flatpak_apps)}"]
                self._run_stream_command(cmd, "apply selected flatpak updates", requires_privilege=True)
        def _clear_listbox(self, listbox):
            if listbox is None:
                return
            child = listbox.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                listbox.remove(child)
                child = nxt

        def _confirm4(self, title, body):
            result = {"done": False, "yes": False}

            def on_resp(dlg, response):
                result["yes"] = response == Gtk4.ResponseType.YES
                result["done"] = True
                dlg.destroy()

            dlg = Gtk4.Dialog(title=title, transient_for=self, modal=True)
            dlg.add_button("No", Gtk4.ResponseType.NO)
            dlg.add_button("Yes", Gtk4.ResponseType.YES)
            area = dlg.get_content_area()
            area.set_spacing(8)
            lbl = Gtk4.Label(label=body)
            lbl.set_wrap(True)
            lbl.set_xalign(0)
            area.append(lbl)
            dlg.connect("response", on_resp)
            dlg.present()
            while not result["done"]:
                while GLib.MainContext.default().iteration(False):
                    pass
                time.sleep(0.01)
            return result["yes"]
        def _create_appimages_page4(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            box.add_css_class("card")
            box.set_vexpand(True)
            toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.appimage_search4 = Gtk4.SearchEntry()
            self.appimage_search4.set_placeholder_text("Filter AppImages by name or path…")
            self.appimage_search4.connect("search-changed", lambda *_: self._filter_appimages4())
            scan_btn = Gtk4.Button(label="Scan")
            scan_btn.set_tooltip_text("Scan entire home directory (~) recursively for .AppImage files")
            scan_btn.connect("clicked", lambda *_: self._load_appimages4())
            import_btn = Gtk4.Button(label="Import AppImage")
            import_btn.connect("clicked", lambda *_: self._import_appimage4())
            launch_btn = Gtk4.Button(label="Launch Selected")
            launch_btn.connect("clicked", lambda *_: self._launch_appimage4())
            remove_btn = Gtk4.Button(label="Remove Selected")
            remove_btn.connect("clicked", lambda *_: self._remove_appimage4())
            toolbar.append(self.appimage_search4)
            for b in (scan_btn, import_btn, launch_btn, remove_btn):
                toolbar.append(b)
            box.append(toolbar)
            self.appimage_stats4 = Gtk4.Label(label="AppImages: 0")
            self.appimage_stats4.set_xalign(0)
            self.appimage_stats4.add_css_class("dim-label")
            box.append(self.appimage_stats4)

            self.appimage_list4 = Gtk4.ListBox()
            self.appimage_list4.connect("row-selected", self._on_appimage_selected4)
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.appimage_list4)
            list_wrap = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL)
            list_wrap.append(sw)
            list_wrap.set_vexpand(True)

            self.appimage_details4 = Gtk4.TextView()
            self.appimage_details4.set_editable(False)
            self.appimage_details4.set_monospace(True)
            self.appimage_details_buf4 = self.appimage_details4.get_buffer()
            self.appimage_details_buf4.set_text("Select an AppImage entry.")
            details_panel = self._create_details_panel4(
                self.appimage_details4, title="AppImage details", min_content_height=140
            )
            split = self._create_vertical_split4(
                list_wrap,
                details_panel,
                top_height=380,
                resize_top=True,
                resize_bottom=False,
            )
            split.set_vexpand(True)
            box.append(split)
            return box
        def _create_details_panel4(self, text_view, title="Details", min_content_height=140):
            """Labeled details block for use inside a Gtk.Paned (drag handle resizes height)."""
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=4)
            header = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            title_lbl = Gtk4.Label(label=title)
            title_lbl.set_xalign(0)
            title_lbl.add_css_class("heading")
            hint = Gtk4.Label(label="Drag the divider above to resize")
            hint.set_xalign(1)
            hint.add_css_class("dim-label")
            header.append(title_lbl)
            header.append(hint)
            box.append(header)
            sw = Gtk4.ScrolledWindow()
            sw.set_policy(Gtk4.PolicyType.AUTOMATIC, Gtk4.PolicyType.AUTOMATIC)
            sw.set_min_content_height(min_content_height)
            sw.set_child(text_view)
            sw.set_vexpand(True)
            box.append(sw)
            box.set_vexpand(True)
            box.set_hexpand(True)
            return box
        def _create_horizontal_split4(
            self,
            start_widget,
            end_widget,
            start_width=520,
            resize_start=True,
            resize_end=False,
        ):
            """Horizontal Gtk.Paned: start | end (drag divider to resize)."""
            paned = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)
            paned.set_start_child(start_widget)
            paned.set_end_child(end_widget)
            paned.set_position(start_width)
            paned.set_resize_start_child(resize_start)
            paned.set_resize_end_child(resize_end)
            paned.set_shrink_start_child(True)
            paned.set_shrink_end_child(True)
            paned.set_vexpand(True)
            paned.set_hexpand(True)
            return paned
        def _create_install_workspace4(self):
            panel = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            panel.set_margin_top(10)
            panel.set_margin_bottom(10)
            panel.set_margin_start(12)
            panel.set_margin_end(12)
            panel.set_vexpand(True)

            search_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            self.search_entry4 = Gtk4.SearchEntry()
            self.search_entry4.set_placeholder_text("Search pacman, AUR, Flatpak, AppImage…")
            self.search_entry4.set_hexpand(True)
            self.search_entry4.connect("activate", lambda *_: self._search_packages())
            self.search_entry4.connect("search-changed", lambda *_: self._schedule_search4())
            self.install_search_spinner4 = Gtk4.Spinner()
            self.install_search_spinner4.add_css_class("search-spinner")
            search_btn = Gtk4.Button(label="Search")
            search_btn.add_css_class("suggested-action")
            search_btn.connect("clicked", lambda *_: self._search_packages())
            search_row.append(self.search_entry4)
            search_row.append(self.install_search_spinner4)
            search_row.append(search_btn)
            panel.append(search_row)

            mode_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.search_type4 = Gtk4.DropDown.new_from_strings(list(INSTALL_COMBO_TO_KEY.keys()))
            self.search_type4.set_tooltip_text("Search scope")
            self.search_type4.connect("notify::selected-item", self._on_install_mode_changed4)
            refresh_btn = Gtk4.Button(icon_name="view-refresh-symbolic")
            refresh_btn.set_tooltip_text("Reload list for current source")
            refresh_btn.connect("clicked", lambda *_: self._refresh_install_list4())
            install_local_btn = Gtk4.Button(label="Local PKG")
            install_local_btn.connect("clicked", lambda *_: self._install_local_package4())
            mode_row.append(self.search_type4)
            mode_row.append(refresh_btn)
            mode_row.append(install_local_btn)
            mode_row.set_hexpand(True)
            panel.append(mode_row)

            filter_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.pkg_filter_entry4 = Gtk4.SearchEntry()
            self.pkg_filter_entry4.set_placeholder_text("Filter results…")
            self.pkg_filter_entry4.set_hexpand(True)
            self.pkg_filter_entry4.connect("search-changed", self._on_pkg_client_filter_changed4)
            self.pkg_installed_check4 = Gtk4.CheckButton(label="Installed only")
            self.pkg_installed_check4.connect("toggled", self._on_pkg_client_filter_changed4)
            export_visible_btn = Gtk4.Button(icon_name="document-save-symbolic")
            export_visible_btn.set_tooltip_text("Export visible list")
            export_visible_btn.connect("clicked", lambda *_: self._export_visible_packages4())
            filter_row.append(self.pkg_filter_entry4)
            filter_row.append(self.pkg_installed_check4)
            filter_row.append(export_visible_btn)
            panel.append(filter_row)

            self.install_stats_label4 = Gtk4.Label(label="Results: 0")
            self.install_stats_label4.set_xalign(0)
            self.install_stats_label4.add_css_class("dim-label")
            panel.append(self.install_stats_label4)

            body = Gtk4.Paned.new(Gtk4.Orientation.VERTICAL)
            body.set_vexpand(True)
            body.set_resize_start_child(True)
            body.set_resize_end_child(False)
            body.set_shrink_start_child(True)
            body.set_shrink_end_child(True)
            self.install_main_paned4 = body
            panel.append(body)

            table_wrap = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL)
            table_wrap.set_vexpand(True)
            self.pkg_store4 = Gio4.ListStore.new(PackageRow4)
            self.pkg_selection4 = Gtk4.SingleSelection(model=self.pkg_store4)
            self.pkg_selection4.set_can_unselect(True)
            self.pkg_selection4.connect("notify::selected-item", self._on_pkg_table_selected4)
            self.pkg_table4 = Gtk4.ColumnView(model=self.pkg_selection4)
            self.pkg_table4.set_vexpand(True)
            self.pkg_table4.append_column(self._create_text_column4("Name", "name", expand=True))
            self.pkg_table4.append_column(self._create_text_column4("Version", "version"))
            self.pkg_table4.append_column(self._create_text_column4("Repo", "repository"))
            self.pkg_table4.append_column(self._create_text_column4("In", "installed"))
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.pkg_table4)
            table_wrap.append(sw)
            body.set_start_child(table_wrap)

            bottom = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            action_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.install_btn4 = Gtk4.Button(label="Install")
            self.install_btn4.connect("clicked", lambda *_: self._run_pkg_action("install"))
            self.remove_btn4 = Gtk4.Button(label="Remove")
            self.remove_btn4.connect("clicked", lambda *_: self._run_pkg_action("remove"))
            self.install_btn4.set_sensitive(False)
            self.remove_btn4.set_sensitive(False)
            action_row.append(self.install_btn4)
            action_row.append(self.remove_btn4)
            bottom.append(action_row)

            details_split = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)
            details_split.set_vexpand(True)
            self.install_right_paned4 = details_split
            self.details_view4 = Gtk4.TextView()
            self.details_view4.set_editable(False)
            self.details_view4.set_monospace(True)
            self.details_buf4 = self.details_view4.get_buffer()
            self.details_buf4.set_text("Select a package to view details.")
            details_panel = self._create_details_panel4(
                self.details_view4, title="Details", min_content_height=80
            )
            self.output_view4 = Gtk4.TextView()
            self.output_view4.set_editable(False)
            self.output_view4.set_monospace(True)
            self.output_buf4 = self.output_view4.get_buffer()
            output_panel = self._create_details_panel4(
                self.output_view4, title="Output", min_content_height=60
            )
            details_split.set_start_child(details_panel)
            details_split.set_end_child(output_panel)
            bottom.append(details_split)
            body.set_end_child(bottom)

            body.connect("realize", lambda *_: GLib.idle_add(self._apply_install_pane_ratio4))
            GLib.idle_add(self._apply_install_pane_ratio4)
            return panel
        def _create_repos_page_gtk4(self):
            pane = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)

            left = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            title = Gtk4.Label(label="Repositories")
            title.set_xalign(0)
            title.add_css_class("section-title")
            left.append(title)
            self.repo_list4 = Gtk4.ListBox()
            self.repo_list4.connect("row-selected", self._on_repo_selected4)
            swl = Gtk4.ScrolledWindow()
            swl.set_child(self.repo_list4)
            swl.set_vexpand(True)
            left.append(swl)
            pane.set_start_child(left)

            right = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            right_title = Gtk4.Label(label="Repository Packages")
            right_title.set_xalign(0)
            right_title.add_css_class("section-title")
            right.append(right_title)
            repo_filter_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.repo_pkg_filter_entry4 = Gtk4.SearchEntry()
            self.repo_pkg_filter_entry4.set_placeholder_text("Filter repository packages...")
            self.repo_pkg_filter_entry4.connect("search-changed", lambda *_: self._refresh_repo_pkg_list4())
            self.repo_installed_only_check4 = Gtk4.CheckButton(label="Installed only")
            self.repo_installed_only_check4.connect("toggled", lambda *_: self._refresh_repo_pkg_list4())
            repo_filter_row.append(self.repo_pkg_filter_entry4)
            repo_filter_row.append(self.repo_installed_only_check4)
            right.append(repo_filter_row)
            actions = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.repo_install_btn4 = Gtk4.Button(label="Install")
            self.repo_install_btn4.connect("clicked", lambda *_: self._repo_install_selected4())
            self.repo_remove_btn4 = Gtk4.Button(label="Remove")
            self.repo_remove_btn4.connect("clicked", lambda *_: self._repo_remove_selected4())
            self.repo_export_btn4 = Gtk4.Button(label="Export List")
            self.repo_export_btn4.connect("clicked", lambda *_: self._repo_export_selected4())
            self.repo_batch_install_btn4 = Gtk4.Button(label="Install Checked")
            self.repo_batch_install_btn4.connect("clicked", lambda *_: self._repo_batch_install4())
            self.repo_batch_remove_btn4 = Gtk4.Button(label="Remove Checked")
            self.repo_batch_remove_btn4.connect("clicked", lambda *_: self._repo_batch_remove4())
            self.repo_install_btn4.set_tooltip_text("Install selected repository package")
            self.repo_remove_btn4.set_tooltip_text("Remove selected repository package")
            self.repo_export_btn4.set_tooltip_text("Export package names of current repository")
            actions.append(self.repo_install_btn4)
            actions.append(self.repo_remove_btn4)
            actions.append(self.repo_batch_install_btn4)
            actions.append(self.repo_batch_remove_btn4)
            actions.append(self.repo_export_btn4)
            right.append(actions)
            self.repo_pkg_list4 = Gtk4.ListBox()
            self.repo_pkg_list4.connect("row-selected", self._on_repo_pkg_selected4)
            swr = Gtk4.ScrolledWindow()
            swr.set_vexpand(True)
            swr.set_child(self.repo_pkg_list4)
            right.append(swr)
            self.repo_pkg_details4 = Gtk4.TextView()
            self.repo_pkg_details4.set_editable(False)
            self.repo_pkg_details4.set_monospace(True)
            self.repo_pkg_details_buf4 = self.repo_pkg_details4.get_buffer()
            self.repo_pkg_details_buf4.set_text("Select a repository package to preview details.")
            swd = Gtk4.ScrolledWindow()
            swd.set_vexpand(True)
            swd.set_child(self.repo_pkg_details4)
            right.append(swd)
            pane.set_end_child(right)
            return pane
        def _create_system_workspace(self):
            scroll = Gtk4.ScrolledWindow()
            scroll.set_policy(Gtk4.PolicyType.NEVER, Gtk4.PolicyType.AUTOMATIC)
            scroll.set_vexpand(True)
            outer = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=10)
            outer.set_margin_top(6)
            outer.set_margin_bottom(6)
            outer.set_margin_start(6)
            outer.set_margin_end(6)
            outer.add_css_class("card")
            scroll.set_child(outer)

            head = Gtk4.Label(label="System maintenance, diagnostics, and logs")
            head.add_css_class("section-title")
            head.set_xalign(0)
            outer.append(head)

            def btn_row(pairs):
                row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
                row.set_homogeneous(True)
                for label, handler in pairs:
                    b = Gtk4.Button(label=label)
                    b.set_tooltip_text(label)
                    b.connect("clicked", handler)
                    row.append(b)
                outer.append(row)

            btn_row(
                (
                    ("Full upgrade", lambda *_: self._gtk4_maybe_run_stream("update")),
                    ("Sync DB (-Sy)", lambda *_: self._gtk4_maybe_run_stream("sync")),
                    ("Clean cache (-Sc)", lambda *_: self._gtk4_maybe_run_stream("clean")),
                    ("Deep clean (-Scc)", lambda *_: self._gtk4_maybe_run_stream("deepclean")),
                )
            )
            btn_row(
                (
                    ("Sync file DB (-Fy)", lambda *_: self._gtk4_maybe_run_stream("fy")),
                    ("Remove orphans", lambda *_: self._gtk4_maybe_run_stream("orphans")),
                    ("AUR full upgrade", lambda *_: self._gtk4_aur_full_upgrade()),
                    ("Verify files (-Qk)", lambda *_: self._gtk4_diag_cmd(["pacman", "-Qk"], "pacman -Qk", 600)),
                )
            )
            btn_row(
                (
                    ("DB check (-Dk)", lambda *_: self._gtk4_diag_cmd(["pacman", "-Dk"], "pacman -Dk", 120)),
                    ("Reflector mirrors", lambda *_: self._gtk4_reflector()),
                    ("Refresh keyring", lambda *_: self._gtk4_maybe_run_stream("keyring")),
                    ("Paccache -k2", lambda *_: self._gtk4_maybe_run_stream("paccache")),
                )
            )
            btn_row(
                (
                    ("systemctl daemon-reload", lambda *_: self._gtk4_maybe_run_stream("daemon_reload")),
                    ("User daemon-reload", lambda *_: self._gtk4_user_daemon_reload()),
                    ("Refresh installed list", lambda *_: self._load_installed_packages()),
                    ("Dependency check (-Dk && -Qk)", lambda *_: self._gtk4_maybe_run_stream("fixdeps")),
                )
            )

            sep1 = Gtk4.Label(label="Diagnostics (below log)")
            sep1.set_xalign(0)
            sep1.add_css_class("dim-label")
            outer.append(sep1)
            btn_row(
                (
                    ("checkupdates", lambda *_: self._gtk4_checkupdates()),
                    ("Foreign (-Qm)", lambda *_: self._gtk4_diag_cmd(["pacman", "-Qm"], "pacman -Qm", 120)),
                    ("Native explicit (-Qne)", lambda *_: self._gtk4_diag_cmd(["pacman", "-Qne"], "pacman -Qne", 120)),
                    ("systemctl --failed", lambda *_: self._gtk4_diag_cmd(["systemctl", "--failed", "--no-pager", "--no-legend"], "systemctl --failed", 60)),
                )
            )
            btn_row(
                (
                    ("Journal errors", lambda *_: self._gtk4_diag_cmd(["journalctl", "-p", "err", "-n", "120", "--no-pager"], "journalctl errors", 90)),
                    (".pacnew scan", lambda *_: self._gtk4_pacnew_scan()),
                    ("ip -br a", lambda *_: self._gtk4_diag_cmd(["ip", "-br", "a"], "ip -br a", 20)),
                    ("uname -a", lambda *_: self._gtk4_diag_cmd(["uname", "-a"], "uname -a", 10)),
                )
            )
            btn_row(
                (
                    ("df -h / /var /home", lambda *_: self._gtk4_df_diag()),
                    ("free -h", lambda *_: self._gtk4_diag_cmd(["free", "-h"], "free -h", 15)),
                    ("pacdiff --safe", lambda *_: self._gtk4_pacdiff_safe()),
                    ("pkgfile -u", lambda *_: self._gtk4_maybe_run_stream("pkgfile_u")),
                )
            )

            sep2 = Gtk4.Label(label="Optional: Flatpak / FWUPD")
            sep2.set_xalign(0)
            sep2.add_css_class("dim-label")
            outer.append(sep2)
            fp_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            fp_row.set_homogeneous(True)
            if command_exists("flatpak"):
                bf1 = Gtk4.Button(label="flatpak --version")
                bf1.connect("clicked", lambda *_: self._gtk4_diag_cmd(["flatpak", "--version"], "flatpak --version", 20))
                bf2 = Gtk4.Button(label="flatpak repair")
                bf2.connect("clicked", lambda *_: self._gtk4_flatpak_repair())
                bf3 = Gtk4.Button(label="flatpak update -y")
                bf3.connect("clicked", lambda *_: self._gtk4_flatpak_update())
                bf4 = Gtk4.Button(label="flatpak unused -y")
                bf4.connect("clicked", lambda *_: self._gtk4_flatpak_unused())
                fp_row.append(bf1)
                fp_row.append(bf2)
                fp_row.append(bf3)
                fp_row.append(bf4)
            if command_exists("fwupdmgr"):
                bf5 = Gtk4.Button(label="fwupdmgr devices")
                bf5.connect(
                    "clicked",
                    lambda *_: self._gtk4_diag_cmd(["fwupdmgr", "get-devices", "--no-pager"], "fwupdmgr get-devices", 120),
                )
                bf6 = Gtk4.Button(label="fwupdmgr updates")
                bf6.connect(
                    "clicked",
                    lambda *_: self._gtk4_diag_cmd(["fwupdmgr", "get-updates", "--no-pager"], "fwupdmgr get-updates", 120),
                )
                fp_row.append(bf5)
                fp_row.append(bf6)
            if fp_row.get_first_child() is not None:
                outer.append(fp_row)

            sep3 = Gtk4.Label(label="Advanced / boot")
            sep3.set_xalign(0)
            sep3.add_css_class("dim-label")
            outer.append(sep3)
            btn_row(
                (
                    ("mkinitcpio -P", lambda *_: self._gtk4_maybe_run_stream("mkinitcpio")),
                    ("grub-mkconfig", lambda *_: self._gtk4_grub_mkconfig()),
                    ("Backup pacman DB", lambda *_: self._gtk4_backup_pacman_db()),
                    ("Export full (-Q)", lambda *_: self._gtk4_export_pkg_lists()),
                )
            )
            btn_row((("Export native explicit (-Qne)", lambda *_: self._gtk4_export_native_explicit()),))

            sep4 = Gtk4.Label(label="Files & pacman log")
            sep4.set_xalign(0)
            sep4.add_css_class("dim-label")
            outer.append(sep4)
            fs_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.sys_file_search4 = Gtk4.SearchEntry()
            self.sys_file_search4.set_placeholder_text("pacman -Fs (file in repos)")
            self.sys_file_search4.connect("activate", lambda *_: self._gtk4_file_search())
            bfs = Gtk4.Button(label="Search -Fs")
            bfs.connect("clicked", lambda *_: self._gtk4_file_search())
            self.sys_file_owns4 = Gtk4.Entry()
            self.sys_file_owns4.set_placeholder_text("/usr/bin/bash — pacman -Qo")
            self.sys_file_owns4.connect("activate", lambda *_: self._gtk4_file_owns())
            bfo = Gtk4.Button(label="Who owns")
            bfo.connect("clicked", lambda *_: self._gtk4_file_owns())
            fs_row.append(self.sys_file_search4)
            fs_row.append(bfs)
            fs_row.append(self.sys_file_owns4)
            fs_row.append(bfo)
            outer.append(fs_row)
            log_tool = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            bpl = Gtk4.Button(label="Reload pacman.log tail")
            bpl.connect("clicked", lambda *_: self._gtk4_reload_pacman_log())
            bnews = Gtk4.Button(label="Arch news (RSS)")
            bnews.connect("clicked", lambda *_: self._load_arch_news4())
            log_tool.append(bpl)
            log_tool.append(bnews)
            outer.append(log_tool)

            log_cap = Gtk4.Label(label="System log (mirrors privileged commands from this tab)")
            log_cap.set_xalign(0)
            outer.append(log_cap)
            self.system_log_view4 = Gtk4.TextView()
            self.system_log_view4.set_editable(False)
            self.system_log_view4.set_monospace(True)
            self.system_log_buf4 = self.system_log_view4.get_buffer()
            self.system_log_buf4.set_text("Select a tool above. Output from streaming commands appears here and in the Install tab log.\n")
            lsw = Gtk4.ScrolledWindow()
            lsw.set_min_content_height(200)
            lsw.set_vexpand(True)
            lsw.set_child(self.system_log_view4)
            outer.append(lsw)
            return scroll
        def _create_text_column4(self, title, prop_name, expand=False):
            factory = Gtk4.SignalListItemFactory()

            def setup(_factory, list_item):
                lbl = Gtk4.Label()
                lbl.set_xalign(0)
                lbl.set_wrap(True)
                list_item.set_child(lbl)

            def bind(_factory, list_item):
                item = list_item.get_item()
                lbl = list_item.get_child()
                lbl.set_text(getattr(item, prop_name, "-"))

            factory.connect("setup", setup)
            factory.connect("bind", bind)
            col = Gtk4.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(expand)
            return col
        def _create_updates_workspace4(self):
            panel = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("card")
            panel.set_vexpand(True)

            self.updates_summary_label4 = Gtk4.Label(label="Scanning for updates…")
            self.updates_summary_label4.set_xalign(0)
            self.updates_summary_label4.add_css_class("dim-label")
            panel.append(self.updates_summary_label4)

            toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            toolbar.add_css_class("toolbar")
            self.updates_search4 = Gtk4.SearchEntry()
            self.updates_search4.set_placeholder_text("Filter by name, repo, severity, advisory…")
            self.updates_search4.set_hexpand(True)
            self.updates_search4.connect("search-changed", lambda *_: self._populate_updates_list())
            refresh_btn = Gtk4.Button(label="Refresh")
            refresh_btn.set_tooltip_text("Reload pending updates (use cached metadata)")
            refresh_btn.connect("clicked", lambda *_: self._load_updates(refresh_metadata=False))
            sync_btn = Gtk4.Button(label="Sync & Refresh")
            sync_btn.set_tooltip_text("Sync pacman databases (-Sy), then scan for updates")
            sync_btn.connect("clicked", lambda *_: self._load_updates(refresh_metadata=True))
            select_all_btn = Gtk4.Button(label="Select All")
            select_all_btn.connect("clicked", lambda *_: self._updates_set_selection4(True))
            deselect_btn = Gtk4.Button(label="Clear")
            deselect_btn.connect("clicked", lambda *_: self._updates_set_selection4(False))
            security_btn = Gtk4.Button(label="Security")
            security_btn.set_tooltip_text("Select security-related updates (Critical / Important / Moderate)")
            security_btn.connect("clicked", lambda *_: self._updates_select_security4())
            apply_btn = Gtk4.Button(label="Apply Selected")
            apply_btn.add_css_class("suggested-action")
            apply_btn.connect("clicked", lambda *_: self._apply_selected_updates())
            upgrade_all_btn = Gtk4.Button(label="Upgrade All")
            upgrade_all_btn.connect("clicked", lambda *_: self._run_system_action("update"))
            toolbar.append(self.updates_search4)
            for b in (refresh_btn, sync_btn, select_all_btn, deselect_btn, security_btn, apply_btn, upgrade_all_btn):
                toolbar.append(b)
            panel.append(toolbar)

            self.updates_list4 = Gtk4.ListBox()
            self.updates_list4.set_selection_mode(Gtk4.SelectionMode.SINGLE)
            self.updates_list4.connect("row-selected", self._on_update_selected4)
            swl = Gtk4.ScrolledWindow()
            swl.set_vexpand(True)
            swl.set_child(self.updates_list4)
            list_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL)
            list_box.append(swl)
            list_box.set_vexpand(True)
            self.update_preview4 = Gtk4.TextView()
            self.update_preview4.set_editable(False)
            self.update_preview4.set_monospace(True)
            self.update_preview_buf4 = self.update_preview4.get_buffer()
            self.update_preview_buf4.set_text(
                "Select an update to view version, advisory, and download details.\n\n"
                "Tips:\n"
                "  • Sync & Refresh — after enabling new repos\n"
                "  • Security — pre-select security-related pacman updates\n"
                "  • Apply Selected — upgrade only checked packages"
            )
            preview_panel = self._create_details_panel4(
                self.update_preview4, title="Update details", min_content_height=160
            )
            body = self._create_horizontal_split4(
                list_box,
                preview_panel,
                start_width=480,
                resize_start=True,
                resize_end=False,
            )
            panel.append(body)
            return panel
        def _create_vertical_split4(
            self,
            top_widget,
            bottom_widget,
            top_height=420,
            resize_top=True,
            resize_bottom=False,
        ):
            """Vertical Gtk.Paned: top_widget | bottom_widget (drag divider to resize)."""
            paned = Gtk4.Paned.new(Gtk4.Orientation.VERTICAL)
            paned.set_start_child(top_widget)
            paned.set_end_child(bottom_widget)
            paned.set_position(top_height)
            paned.set_resize_start_child(resize_top)
            paned.set_resize_end_child(resize_bottom)
            paned.set_shrink_start_child(True)
            paned.set_shrink_end_child(True)
            paned.set_vexpand(True)
            paned.set_hexpand(True)
            return paned
        def _export_visible_packages4(self):
            visible = []
            if hasattr(self, "pkg_store4"):
                for i in range(self.pkg_store4.get_n_items()):
                    row = self.pkg_store4.get_item(i)
                    if row and hasattr(row, "raw_item"):
                        visible.append(row.raw_item)
            if not visible:
                self._append_output("No visible packages to export.")
                return
            filename = "pkger-visible-packages.txt"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    for it in visible:
                        f.write(f"{it.get('repository','unknown')}/{it.get('name','-')}  {it.get('version','-')}\n")
                self._append_output(f"Exported visible packages to {filename}")
            except Exception as e:
                self._append_output(f"Export failed: {e}")
        def _filter_appimages4(self):
            if not hasattr(self, "appimage_list4"):
                return
            q = ""
            if hasattr(self, "appimage_search4"):
                q = self.appimage_search4.get_text().strip().lower()
            self._clear_listbox(self.appimage_list4)
            shown = 0
            for it in self.appimages_data4:
                path = self._appimage_path4(it)
                hay = f"{it.get('name','')} {path} {it.get('description','')}".lower()
                if q and q not in hay:
                    continue
                row = Gtk4.ListBoxRow()
                label = Gtk4.Label(label=f"{it.get('name', '-')}  —  {path or it.get('description', '-')}")
                label.set_xalign(0)
                label.set_ellipsize(Gtk4.EllipsizeMode.END)
                row.set_child(label)
                row.appimage_item = it
                self.appimage_list4.append(row)
                shown += 1
            if hasattr(self, "appimage_stats4"):
                self.appimage_stats4.set_text(f"AppImages: {shown}/{len(self.appimages_data4)}")
        def _format_updates_summary4(self, summary):
            summary = summary or {}
            if not summary.get("total"):
                return "System is up to date — no pending pacman or Flatpak updates."
            return (
                f"Pending: {summary.get('total', 0)} total  |  "
                f"pacman: {summary.get('pacman', summary.get('total', 0))}  |  "
                f"Flatpak: {summary.get('flatpak', 0)}  |  "
                f"Security-related: {summary.get('security', 0)}"
            )
        def _get_filtered_repos4(self):
            q = self.repo_filter_entry4.get_text().strip().lower() if hasattr(self, "repo_filter_entry4") else ""
            kind_idx = self.repo_kind_dropdown4.get_selected() if hasattr(self, "repo_kind_dropdown4") else 0
            kind_map = {0: "all", 1: "official", 2: "community", 3: "third-party", 4: "testing", 5: "unknown"}
            kind_target = kind_map.get(kind_idx, "all")
            repos = []
            for repo in self.repo_data4.keys():
                kind = classify_repo_kind(repo)
                if q and q not in repo.lower():
                    continue
                if kind_target != "all" and kind != kind_target:
                    continue
                repos.append(repo)
            return sorted(repos, key=str.lower)
        def _get_textbuf_text4(self, buf):
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            return buf.get_text(start, end, True) or ""
        def _gtk4_append_block(self, title, text, limit=12000):
            body = (text or "").strip()
            if len(body) > limit:
                body = body[:limit] + "\n… (truncated)\n"
            block = f"=== {title} ===\n{body if body else '(empty)'}\n\n"
            buf = getattr(self, "system_log_buf4", None)
            if buf is None:
                self._append_output(f"{title}: {body[:2000]}")
                return

            def apply():
                end = buf.get_end_iter()
                buf.insert(end, block)

            GLib.idle_add(apply)
        def _gtk4_backup_pacman_db(self):
            chooser = Gtk4.FileChooserNative.new(
                "Save pacman local database backup",
                self,
                Gtk4.FileChooserAction.SAVE,
                "Save",
                "Cancel",
            )
            chooser.set_current_name("pacman-local-db.tar.xz")

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if file_obj is None:
                    return
                path = file_obj.get_path()
                if not path:
                    return
                if not self._confirm4("Backup", f"Archive /var/lib/pacman/local to:\n{path}"):
                    return
                inner = f"tar -cJf {shlex.quote(path)} -C /var/lib/pacman local"
                self._run_stream_command(["bash", "-lc", inner], "backup pacman database", requires_privilege=True)

            chooser.connect("response", on_response)
            chooser.show()
        def _gtk4_aur_full_upgrade(self):
            if not AUR_HELPER:
                self._append_output("No AUR helper (install yay or paru).")
                return
            if not self._confirm4(
                "Full update with AUR",
                f"Run {AUR_HELPER} -Syu --noconfirm? The helper handles sudo when needed.",
            ):
                return
            self._run_stream_command(
                ["bash", "-lc", f"{shlex.quote(AUR_HELPER)} -Syu --noconfirm"],
                "full update (AUR)",
                requires_privilege=False,
            )

        def _gtk4_checkupdates(self):
            if not command_exists("checkupdates"):
                self._gtk4_append_block("checkupdates", "Not installed. pacman -S pacman-contrib")
                return
            self._gtk4_diag_cmd(["checkupdates"], "checkupdates", 600)

        def _gtk4_pacnew_scan(self):
            self._gtk4_diag_cmd(
                ["bash", "-lc", r"find /etc -name '*.pacnew' 2>/dev/null | sort"],
                ".pacnew under /etc",
                120,
            )

        def _gtk4_pacdiff_safe(self):
            if not command_exists("pacdiff"):
                self._gtk4_append_block("pacdiff", "Not installed. pacman -S pacman-contrib")
                return
            self._gtk4_diag_cmd(["pacdiff", "--safe"], "pacdiff --safe", 300)

        def _gtk4_reflector(self):
            if not command_exists("reflector"):
                self._append_output("reflector not installed.")
                return
            if not self._confirm4(
                "Reflector",
                "Overwrite /etc/pacman.d/mirrorlist with 20 latest mirrors sorted by rate?",
            ):
                return
            self._run_stream_command(
                [
                    "bash",
                    "-lc",
                    "reflector --latest 20 --sort rate --save /etc/pacman.d/mirrorlist",
                ],
                "reflector",
                requires_privilege=True,
            )

        def _gtk4_df_diag(self):
            def worker():
                parts = []
                for path in ("/", "/var", "/home"):
                    try:
                        r = subprocess.run(["df", "-h", path], capture_output=True, text=True, timeout=25)
                        if r.stdout.strip():
                            parts.append(r.stdout.strip())
                    except Exception as exc:
                        parts.append(f"{path}: {exc}")
                GLib.idle_add(self._gtk4_append_block, "df -h", "\n\n".join(parts))

            threading.Thread(target=worker, daemon=True).start()
        def _gtk4_diag_cmd(self, argv, title, timeout=120):
            def worker():
                try:
                    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                    out = (r.stdout or "") + (r.stderr or "")
                    GLib.idle_add(self._gtk4_append_block, title, out)
                except Exception as exc:
                    GLib.idle_add(self._append_output, f"{title} failed: {exc}")

            threading.Thread(target=worker, daemon=True).start()

        def _gtk4_export_native_explicit(self):
            chooser = Gtk4.FileChooserNative.new(
                "Save native explicit list (-Qne)",
                self,
                Gtk4.FileChooserAction.SAVE,
                "Save",
                "Cancel",
            )
            chooser.set_current_name("native-explicit-packages.txt")

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if file_obj is None:
                    return
                path = file_obj.get_path()
                if not path:
                    return

                def worker():
                    try:
                        r = subprocess.run(["pacman", "-Qne"], capture_output=True, text=True, timeout=180)
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(r.stdout)
                        GLib.idle_add(self._append_output, f"Exported native explicit list to {path}")
                    except Exception as exc:
                        GLib.idle_add(self._append_output, f"Export failed: {exc}")

                threading.Thread(target=worker, daemon=True).start()

            chooser.connect("response", on_response)
            chooser.show()
        def _gtk4_export_pkg_lists(self):
            chooser = Gtk4.FileChooserNative.new(
                "Save package list (-Q)",
                self,
                Gtk4.FileChooserAction.SAVE,
                "Save",
                "Cancel",
            )
            chooser.set_current_name("all-packages.txt")

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if file_obj is None:
                    return
                path = file_obj.get_path()
                if not path:
                    return

                def worker():
                    try:
                        r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=180)
                        with open(path, "w", encoding="utf-8") as f:
                            f.write(r.stdout)
                        GLib.idle_add(self._append_output, f"Exported full list to {path}")
                    except Exception as exc:
                        GLib.idle_add(self._append_output, f"Export failed: {exc}")

                threading.Thread(target=worker, daemon=True).start()

            chooser.connect("response", on_response)
            chooser.show()
        def _gtk4_file_owns(self):
            path = self.sys_file_owns4.get_text().strip() if hasattr(self, "sys_file_owns4") else ""
            if not path:
                return
            self._gtk4_diag_cmd(["pacman", "-Qo", path], f"pacman -Qo {path}", 30)
        def _gtk4_file_search(self):
            q = self.sys_file_search4.get_text().strip() if hasattr(self, "sys_file_search4") else ""
            if not q:
                return

            def worker():
                try:
                    r = subprocess.run(["pacman", "-Fs", q], capture_output=True, text=True, timeout=90)
                    out = (r.stdout or "") + (r.stderr or "")
                    if not out.strip():
                        out = "(no results — run Sync file DB: pacman -Fy)\n"
                    GLib.idle_add(self._gtk4_append_block, f"pacman -Fs {q}", out)
                except Exception as exc:
                    GLib.idle_add(self._append_output, f"pacman -Fs failed: {exc}")

            threading.Thread(target=worker, daemon=True).start()
        def _gtk4_flatpak_repair(self):
            if not command_exists("flatpak"):
                return
            if not self._confirm4("Flatpak", "Run flatpak repair --noninteractive?"):
                return
            self._run_stream_command(
                ["bash", "-lc", "flatpak repair --noninteractive"],
                "flatpak repair",
                requires_privilege=False,
            )
        def _gtk4_flatpak_unused(self):
            if not command_exists("flatpak"):
                return
            if not self._confirm4("Flatpak", "Remove unused flatpak data (flatpak uninstall --unused -y)?"):
                return
            self._run_stream_command(
                ["bash", "-lc", "flatpak uninstall --unused -y"],
                "flatpak remove unused",
                requires_privilege=False,
            )
        def _gtk4_flatpak_update(self):
            if not command_exists("flatpak"):
                return
            if not self._confirm4("Flatpak", "Run flatpak update -y?"):
                return
            self._run_stream_command(
                ["bash", "-lc", "flatpak update -y"],
                "flatpak update",
                requires_privilege=False,
            )
        def _gtk4_grub_mkconfig(self):
            if not command_exists("grub-mkconfig"):
                self._append_output("grub-mkconfig not found.")
                return
            candidates = ["/boot/grub/grub.cfg", "/boot/efi/EFI/grub/grub.cfg"]
            target = next((p for p in candidates if os.path.isfile(p)), "/boot/grub/grub.cfg")
            if not self._confirm4("GRUB", f"Run grub-mkconfig -o {target}?"):
                return
            self._run_stream_command(
                ["bash", "-lc", f"grub-mkconfig -o {shlex.quote(target)}"],
                "grub-mkconfig",
                requires_privilege=True,
            )
        def _gtk4_maybe_run_stream(self, key):
            mapping = {
                "update": (
                    "Full system upgrade (pacman -Syu --noconfirm)?",
                    ["bash", "-lc", "pacman -Syu --noconfirm"],
                    "pacman -Syu",
                    True,
                ),
                "sync": (
                    "Sync databases (pacman -Sy --noconfirm)?",
                    ["bash", "-lc", "pacman -Sy --noconfirm"],
                    "sync databases",
                    True,
                ),
                "clean": (
                    "Clean package cache (pacman -Sc --noconfirm)?",
                    ["bash", "-lc", "pacman -Sc --noconfirm"],
                    "clean cache",
                    True,
                ),
                "deepclean": (
                    "Remove ALL cached packages (pacman -Scc --noconfirm)?",
                    ["bash", "-lc", "pacman -Scc --noconfirm"],
                    "deep clean cache",
                    True,
                ),
                "fy": (
                    "Download file databases for pacman -F (pacman -Fy --noconfirm)?",
                    ["bash", "-lc", "pacman -Fy --noconfirm"],
                    "sync file database",
                    True,
                ),
                "orphans": (
                    "Remove orphan packages (pacman -Qtdq | pacman -Rns --noconfirm -)?",
                    ["bash", "-lc", "pacman -Qtdq | pacman -Rns --noconfirm -"],
                    "remove orphans",
                    True,
                ),
                "fixdeps": (
                    "Run pacman -Dk and pacman -Qk (integrity checks; does not change packages)?",
                    ["bash", "-lc", "pacman -Dk && pacman -Qk"],
                    "dependency checks",
                    False,
                ),
                "keyring": (
                    "Refresh pacman keyring (pacman-key --refresh-keys)? Network required.",
                    ["bash", "-lc", "pacman-key --refresh-keys"],
                    "refresh keyring",
                    True,
                ),
                "paccache": (
                    "Trim package cache keeping last 2 versions (paccache -r -k 2)?\nRequires pacman-contrib.",
                    ["bash", "-lc", "paccache -r -k 2"],
                    "paccache trim",
                    True,
                ),
                "daemon_reload": (
                    "Run systemctl daemon-reload as administrator?",
                    ["bash", "-lc", "systemctl daemon-reload"],
                    "systemd daemon-reload",
                    True,
                ),
                "pkgfile_u": (
                    "Update pkgfile databases (pkgfile -u)?",
                    ["bash", "-lc", "pkgfile -u"],
                    "pkgfile update",
                    True,
                ),
                "mkinitcpio": (
                    "Regenerate ALL initramfs images (mkinitcpio -P)?",
                    ["bash", "-lc", "mkinitcpio -P"],
                    "mkinitcpio -P",
                    True,
                ),
            }
            if key not in mapping:
                self._append_output(f"Unknown system action key: {key}")
                return
            msg, cmd, label, need_root = mapping[key]
            if key == "mkinitcpio" and not command_exists("mkinitcpio"):
                self._append_output("mkinitcpio not found. Install mkinitcpio.")
                return
            if not self._confirm4("Confirm", msg):
                return
            if key == "paccache" and not command_exists("paccache"):
                self._append_output("paccache not found. Install pacman-contrib.")
                return
            if key == "pkgfile_u" and not command_exists("pkgfile"):
                self._append_output("pkgfile not found. Install pkgfile.")
                return
            self._run_stream_command(cmd, label, requires_privilege=need_root)
        def _gtk4_quick_install_mode(self, mode_key):
            try:
                idx = GTK4_INSTALL_MODE_KEYS.index(mode_key)
            except ValueError:
                self._append_output(f"Unknown install mode: {mode_key}")
                return
            self.mode_stack4.set_visible_child_name("install")
            self.search_type4.set_selected(idx)
            if mode_key in INSTALL_SUBSET_KEYS:
                self._search_packages()
        def _gtk4_refresh_all_data4(self):
            self._load_installed_packages()
            self._load_updates()
            self._load_repos()
            self._load_appimages4()
            self._refresh_dashboard()
            self._append_output("Refreshed install list, updates, repositories, and AppImages.")
        def _gtk4_reload_pacman_log(self):
            path = "/var/log/pacman.log"

            def worker():
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    tail = "".join(lines[-500:])
                    GLib.idle_add(self._gtk4_append_block, "pacman.log (tail)", tail)
                except Exception as exc:
                    GLib.idle_add(self._gtk4_append_block, "pacman.log", f"Cannot read {path}: {exc}")

            threading.Thread(target=worker, daemon=True).start()

        def _gtk4_user_daemon_reload(self):
            def worker():
                try:
                    r = subprocess.run(
                        ["systemctl", "--user", "daemon-reload"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    out = "OK\n" if r.returncode == 0 else ((r.stderr or "") + (r.stdout or ""))
                    GLib.idle_add(self._gtk4_append_block, "systemctl --user daemon-reload", out)
                except Exception as exc:
                    GLib.idle_add(self._append_output, f"User daemon-reload failed: {exc}")

            threading.Thread(target=worker, daemon=True).start()
        def _guess_icon_name4(self, item):
            repo = (item.get("repository") or "").lower()
            name = (item.get("pkg") or item.get("name") or "").lower()
            if repo == "flatpak":
                return "flatpak-symbolic"
            if repo == "appimage":
                return "application-x-executable-symbolic"
            if any(k in name for k in ("gnome", "kde", "xfce", "qt", "gtk")):
                return "applications-other-symbolic"
            if any(k in name for k in ("python", "node", "go", "rust", "gcc", "clang")):
                return "applications-development-symbolic"
            if any(k in name for k in ("firefox", "chromium", "brave")):
                return "web-browser-symbolic"
            return "package-x-generic-symbolic"
        def _import_appimage4(self):
            def on_path(src):
                if not src:
                    return
                dst_dir = Path.home() / "Applications"
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst = dst_dir / Path(src).name
                try:
                    subprocess.run(["cp", "-f", src, str(dst)], check=False)
                    subprocess.run(["chmod", "+x", str(dst)], check=False)
                    self._append_output(f"Imported AppImage: {dst}")
                    self._load_appimages4()
                except Exception as e:
                    self._append_output(f"Import AppImage failed: {e}")

            gtk4_run_open_file(self, "Import AppImage", ["*.AppImage", "*.appimage"], on_path)
        def _install_local_package4(self):
            chooser = Gtk4.FileChooserNative.new(
                "Select local package",
                self,
                Gtk4.FileChooserAction.OPEN,
                "Install",
                "Cancel",
            )
            filt = Gtk4.FileFilter()
            filt.set_name("Arch package files")
            filt.add_pattern("*.pkg.tar.zst")
            filt.add_pattern("*.pkg.tar.xz")
            chooser.add_filter(filt)

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if file_obj is None:
                    return
                path = file_obj.get_path()
                if not path:
                    return
                self.local_pkg_path = path
                if hasattr(self, "local_pkg_path_entry"):
                    self.local_pkg_path_entry.set_text(path)
                self._append_output(f"Selected local package: {path}")
                self._run_stream_command(
                    ["bash", "-lc", f"pacman -U --noconfirm {shlex.quote(path)}"],
                    "install local package",
                    requires_privilege=True,
                )

            chooser.connect("response", on_response)
            chooser.show()
        def _launch_appimage4(self):
            item = getattr(self, "selected_appimage4", None)
            if not item:
                self._append_output("Select an AppImage first.")
                return
            p = self._appimage_path4(item)
            if not p or not os.path.isfile(p):
                self._append_output("Invalid AppImage path.")
                return
            if not os.access(p, os.X_OK):
                try:
                    os.chmod(p, os.stat(p).st_mode | 0o111)
                except OSError:
                    pass
            self._append_output(f"Launching AppImage: {p}")
            subprocess.Popen([p], start_new_session=True)
        def _load_appimages(self):
            return self._load_appimages4()
        def _load_appimages4(self):
            self._set_status("Scanning AppImages...")
            self.appimages_data4 = scan_appimages("")
            self._clear_listbox(self.appimage_list4)
            for it in self.appimages_data4:
                row = Gtk4.ListBoxRow()
                label = Gtk4.Label(label=f"{it['name']}  ({it['description'].replace('Local AppImage: ', '')})")
                label.set_xalign(0)
                row.set_child(label)
                row.appimage_item = it
                self.appimage_list4.append(row)
            self._set_status(f"Found {len(self.appimages_data4)} AppImages")
        def _load_installed_packages(self):
            self._set_status("Loading installed packages...")

            def worker():
                out = ""
                try:
                    r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=40)
                    out = r.stdout
                except Exception:
                    out = ""
                items = []
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        items.append({
                            "name": parts[0],
                            "repo": "installed",
                            "version": parts[1],
                            "description": "",
                        })
                GLib.idle_add(self._populate_pkg_list, items)
                GLib.idle_add(self._set_status, f"Loaded {len(items)} installed packages")
                GLib.idle_add(self._refresh_dashboard)

            threading.Thread(target=worker, daemon=True).start()
        def _load_repos(self):
            self._set_status("Loading repositories...")

            def worker():
                data = {}
                try:
                    r = subprocess.run(["pacman", "-Sl"], capture_output=True, text=True, timeout=70)
                    for line in r.stdout.splitlines():
                        parts = line.split()
                        if len(parts) >= 2:
                            repo, name = parts[0], parts[1]
                            installed = "[installed]" in line
                            data.setdefault(repo, []).append({"name": name, "installed": installed})
                except Exception:
                    pass
                self.repo_data4 = data
                GLib.idle_add(self._populate_repos_list)
                GLib.idle_add(self._set_status, f"Loaded {len(data)} repositories")
                GLib.idle_add(self._refresh_dashboard)

            threading.Thread(target=worker, daemon=True).start()
        def _load_updates(self, refresh_metadata=False):
            if refresh_metadata:
                self._load_updates_sync()
                return
            self._set_status("Loading updates...")

            def worker():
                items = []
                try:
                    r = subprocess.run(["pacman", "-Qu"], capture_output=True, text=True, timeout=50)
                    for line in r.stdout.splitlines():
                        p = line.split()
                        if len(p) >= 4 and p[2] == "->":
                            items.append({"name": p[0], "from": p[1], "to": p[3], "selected": False, "source": "pacman"})
                except Exception:
                    pass
                if command_exists("flatpak"):
                    try:
                        rf = subprocess.run(
                            ["flatpak", "remote-ls", "--updates", "--columns=application,version", "flathub"],
                            capture_output=True,
                            text=True,
                            timeout=40,
                        )
                        for line in rf.stdout.splitlines():
                            parts = [p.strip() for p in line.split("\t") if p.strip()]
                            if not parts:
                                continue
                            app = parts[0]
                            ver = parts[1] if len(parts) > 1 else "update"
                            items.append({"name": app, "from": "-", "to": ver, "selected": False, "source": "flatpak"})
                    except Exception:
                        pass
                self.updates_data4 = items
                GLib.idle_add(self._populate_updates_list)
                GLib.idle_add(self._set_status, f"Loaded {len(items)} updates")
                GLib.idle_add(self._refresh_dashboard)

            threading.Thread(target=worker, daemon=True).start()

        def _load_updates_sync(self):
            self._set_status("Syncing databases…")
            def worker():
                try:
                    subprocess.run(["pacman", "-Sy", "--noconfirm"], capture_output=True, text=True, timeout=120)
                except Exception:
                    pass
                GLib.idle_add(lambda: self._load_updates())
            threading.Thread(target=worker, daemon=True).start()


        def _on_appimage_selected4(self, _list, row):
            if row is None:
                self.appimage_details_buf4.set_text("Select an AppImage entry.")
                self.selected_appimage4 = None
                return
            self.selected_appimage4 = row.appimage_item
            p = self._appimage_path4(self.selected_appimage4)
            self.appimage_details_buf4.set_text(
                f"Name: {self.selected_appimage4.get('name','-')}\n"
                f"Type: AppImage\n"
                f"Size: {self.selected_appimage4.get('version', '-')}\n"
                f"Path: {p}\n"
            )
        def _on_install_mode_changed4(self, *_args):
            idx = self.search_type4.get_selected()
            if idx is None or idx < 0:
                return
            if idx >= len(GTK4_INSTALL_MODE_KEYS):
                return
            mode = GTK4_INSTALL_MODE_KEYS[idx]
            if mode in INSTALL_SUBSET_KEYS:
                self._search_packages()
        def _on_pkg_client_filter_changed4(self, *_):
            self._refresh_pkg_list_view4()
        def _on_pkg_selected(self, _listbox, row):
            if row is None:
                self.current_selection = None
                self.details_buf4.set_text("Select a package to view details.")
                self.install_btn4.set_sensitive(False)
                self.remove_btn4.set_sensitive(False)
                return
            item = row.pkg
            self.current_selection = item
            pkg_name = item.get("name") or item.get("pkg") or "-"
            repo_name = item.get("repo") or item.get("repository") or "unknown"
            self._set_status(f"Loading details for {pkg_name}...")
            if hasattr(self, "pkg_stack"):
                self.pkg_stack.set_visible_child_name("details")
            installed = bool(item.get("installed", False))
            self.install_btn4.set_sensitive(not installed)
            self.remove_btn4.set_sensitive(installed)

            def worker():
                txt = f"Name: {pkg_name}\nRepository: {repo_name}\nVersion: {item.get('version', '-')}\n\n"
                try:
                    r = None
                    if str(repo_name).lower() == "flatpak" and command_exists("flatpak"):
                        r = subprocess.run(
                            ["flatpak", "info", pkg_name],
                            capture_output=True,
                            text=True,
                            timeout=40,
                        )
                    elif str(repo_name).lower() == "aur" and AUR_HELPER:
                        r = subprocess.run(
                            [AUR_HELPER, "-Si", pkg_name],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    else:
                        r = subprocess.run(
                            ["pacman", "-Si", pkg_name],
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    if r and r.returncode == 0 and r.stdout.strip():
                        # Parse pacman -Si output for more details
                        details_lines = r.stdout.splitlines()
                        parsed_details = {}
                        current_key = None
                        for d_line in details_lines:
                            if ":" in d_line and not d_line.startswith(" "):
                                key, value = d_line.split(":", 1)
                                current_key = key.strip().lower().replace(" ", "_")
                                parsed_details[current_key] = value.strip()
                            elif current_key and d_line.startswith(" "):
                                parsed_details[current_key] += " " + d_line.strip()

                        txt = f"Name: {parsed_details.get('name', pkg_name)}\n"
                        txt += f"Repository: {repo_name}\n"
                        txt += f"Version: {parsed_details.get('version', item.get('version', '-'))}\n"
                        txt += f"Description: {parsed_details.get('description', item.get('description', '-'))}\n"
                        txt += f"URL: {parsed_details.get('url', '-')}\n"
                        txt += f"Licenses: {parsed_details.get('licenses', '-')}\n"
                        txt += f"Groups: {parsed_details.get('groups', '-')}\n"
                        txt += f"Provides: {parsed_details.get('provides', '-')}\n"
                        txt += f"Depends On: {parsed_details.get('depends_on', '-')}\n"
                        txt += f"Optional Deps: {parsed_details.get('optional_deps', '-')}\n"
                        txt += f"Conflicts With: {parsed_details.get('conflicts_with', '-')}\n"
                        txt += f"Replaces: {parsed_details.get('replaces', '-')}\n"
                        txt += f"Download Size: {parsed_details.get('download_size', '-')}\n"
                        txt += f"Installed Size: {parsed_details.get('installed_size', '-')}\n"
                        txt += f"Packager: {parsed_details.get('packager', '-')}\n"
                        txt += f"Build Date: {parsed_details.get('build_date', '-')}\n"
                        txt += f"Install Date: {parsed_details.get('install_date', '-')}\n"
                        txt += f"Validated By: {parsed_details.get('validated_by', '-')}\n"
                    else:
                        r2 = subprocess.run(
                            ["pacman", "-Qi", pkg_name],
                            capture_output=True,
                            text=True,
                            timeout=20,
                        )
                        if r2.returncode == 0 and r2.stdout.strip():
                            txt = r2.stdout
                except Exception as exc:
                    log.debug("GTK4 package details failed: %s", exc, exc_info=True)
                GLib.idle_add(self.details_buf4.set_text, txt)
                GLib.idle_add(self._set_status, f"Details loaded for {pkg_name}")

            threading.Thread(target=worker, daemon=True).start()
        def _on_pkg_table_selected4(self, *_):
            if not hasattr(self, "pkg_selection4"):
                return
            row = self.pkg_selection4.get_selected_item()
            if row is None:
                self._on_pkg_selected(None, None)
                return
            item = getattr(row, "raw_item", None)
            if not item:
                return
            holder = type("PkgRowHolder", (), {"pkg": item})()
            self._on_pkg_selected(None, holder)
        def _on_repo_pkg_table_selected4(self, *_):
            if not hasattr(self, "repo_pkg_selection4"):
                return
            row = self.repo_pkg_selection4.get_selected_item()
            if row is None:
                self.selected_repo_pkg4 = None
                self.selected_repo_pkg_item4 = None
                if hasattr(self, "repo_install_btn4"):
                    self.repo_install_btn4.set_sensitive(False)
                    self.repo_remove_btn4.set_sensitive(False)
                return
            item = getattr(row, "raw_item", None) or {}
            name = item.get("name") or row.name
            self.selected_repo_pkg4 = name
            self.selected_repo_pkg_item4 = item
            installed = bool(item.get("installed"))
            if hasattr(self, "repo_install_btn4"):
                self.repo_install_btn4.set_sensitive(not installed)
                self.repo_remove_btn4.set_sensitive(installed)
            repo = self.selected_repo4 or "?"
            self._set_status(f"Selected {name} from {repo}")

            def worker():
                txt = (
                    f"Repository: {repo}\n"
                    f"Package: {name}\n"
                    f"Version: {item.get('version', '-')}\n"
                    f"Installed: {'yes' if installed else 'no'}\n"
                    f"Description: {item.get('description', '-')}\n\n"
                )
                try:
                    r = subprocess.run(
                        ["pacman", "-Si", name],
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    if r.returncode == 0 and (r.stdout or "").strip():
                        txt += r.stdout
                except Exception:
                    pass
                GLib.idle_add(self.repo_pkg_details_buf4.set_text, txt)

            threading.Thread(target=worker, daemon=True).start()

        def _on_update_selected4(self, _list, row):
            if row is None:
                self.update_preview_buf4.set_text("Select an update for details.")
                return
            item = row.update_item
            self.update_preview_buf4.set_text(
                f"Package: {item['name']}\nCurrent: {item['from']}\nNew: {item['to']}\n\n"
                "Use Update Selected or Upgrade All to apply."
            )
        def _ops_run_interactive_command4(self, *_args, command_text=None):
            cmd_text = (command_text if command_text is not None else self.ops_cmd_entry4.get_text()).strip()
            if not cmd_text:
                return
            self._append_output(f"$ {cmd_text}")
            privileged = (
                "sudo " in cmd_text
                or cmd_text.startswith("pacman -S")
                or cmd_text.startswith("pacman -R")
                or cmd_text.startswith("pacman -U")
                or cmd_text.startswith("pacman -Sy")
                or cmd_text.startswith("pacman -Syu")
                or cmd_text.startswith("flatpak update")
            )
            self._run_stream_command(
                ["bash", "-lc", cmd_text],
                f"interactive: {cmd_text}",
                requires_privilege=privileged,
            )
        def _parse_search_output(self, output):
            items = []
            lines = output.splitlines()
            i = 0
            while i < len(lines):
                ln = lines[i].strip()
                if ln and not lines[i].startswith(" "):
                    parts = ln.split(" ", 1)
                    if len(parts) >= 2:
                        full = parts[0]
                        repo = full.split("/")[0] if "/" in full else "unknown"
                        name = full.split("/")[-1]
                        ver = parts[1]
                        desc = ""
                        if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                            desc = lines[i + 1].strip()
                            i += 1
                        items.append({"name": name, "repo": repo, "version": ver, "description": desc})
                i += 1
            return items
        def _populate_pkg_list(self, items):
            self.packages_cache = list(items or [])
            self._sort_packages_cache4()
            self._refresh_pkg_list_view4()
        def _populate_repos_list(self):
            self._clear_listbox(self.repo_list4)
            for repo in sorted(self.repo_data4.keys()):
                row = Gtk4.ListBoxRow()
                label = Gtk4.Label(label=f"{repo} ({len(self.repo_data4[repo])})")
                label.set_xalign(0)
                row.set_child(label)
                row.repo_name = repo
                self.repo_list4.append(row)

        def _on_repo_selected4(self, _list, row):
            if row is None:
                return
            repo = row.repo_name
            self.selected_repo4 = repo
            self.selected_repo_pkg4 = None
            self.selected_repo_pkg_item4 = None
            self.selected_repo_pkg_names4.clear()
            self.repo_pkg_results4 = []
            self._refresh_repo_pkg_list4()

        def _refresh_repo_pkg_list4(self):
            if not self.selected_repo4:
                return
            repo = self.selected_repo4
            q = self.repo_pkg_filter_entry4.get_text().strip().lower() if hasattr(self, "repo_pkg_filter_entry4") else ""
            installed_only = self.repo_installed_only_check4.get_active() if hasattr(self, "repo_installed_only_check4") else False
            self._clear_listbox(self.repo_pkg_list4)
            shown = 0
            for it in self.repo_data4.get(repo, [])[:1000]:
                name = it["name"] if isinstance(it, dict) else str(it)
                inst = bool(it.get("installed", False)) if isinstance(it, dict) else False
                if q and q not in name.lower():
                    continue
                if installed_only and not inst:
                    continue
                row_box = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
                chk = Gtk4.CheckButton()
                chk.set_active(name in self.selected_repo_pkg_names4)
                chk.connect("toggled", lambda b, n=name: self._toggle_repo_pkg_checked4(n, b.get_active()))
                lbl = Gtk4.Label(label=name)
                lbl.set_xalign(0)
                row_box.append(chk)
                row_box.append(lbl)
                r = Gtk4.ListBoxRow()
                r.set_child(row_box)
                r.pkg_name = name
                self.repo_pkg_list4.append(r)
                shown += 1
            self._set_status(f"Showing {shown} packages in {repo}")
            if hasattr(self, "repo_pkg_details_buf4"):
                self.repo_pkg_details_buf4.set_text(
                    f"Repository: {repo}\nPackages shown: {shown}\nChecked: {len(self.selected_repo_pkg_names4)}\n\n"
                    "Select a package for full details."
                )

        def _toggle_repo_pkg_checked4(self, name, active):
            if active:
                self.selected_repo_pkg_names4.add(name)
            else:
                self.selected_repo_pkg_names4.discard(name)

        def _on_repo_pkg_selected4(self, _list, row):
            if row is None or not self.selected_repo4:
                self.selected_repo_pkg4 = None
                return
            self.selected_repo_pkg4 = row.pkg_name
            repo = self.selected_repo4
            pkg = self.selected_repo_pkg4
            self._set_status(f"Loading {repo}/{pkg} details...")

            def worker():
                details = f"{repo}/{pkg}\n"
                try:
                    r = subprocess.run(["pacman", "-Si", pkg], capture_output=True, text=True, timeout=25)
                    if r.returncode == 0 and r.stdout.strip():
                        details = r.stdout
                except Exception:
                    pass
                GLib.idle_add(self.repo_pkg_details_buf4.set_text, details)
                GLib.idle_add(self._set_status, f"Loaded {repo}/{pkg} details")

            threading.Thread(target=worker, daemon=True).start()

        def _populate_updates_list(self):
            self._clear_listbox(self.updates_list4)
            for item in self.updates_data4:
                row_box = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
                chk = Gtk4.CheckButton()
                chk.connect("toggled", lambda b, it=item: it.update({"selected": b.get_active()}))
                label = Gtk4.Label(label=f"{item['name']}: {item['from']} -> {item['to']}")
                label.set_xalign(0)
                label.set_hexpand(True)
                row_box.append(chk)
                row_box.append(label)
                row = Gtk4.ListBoxRow()
                row.set_child(row_box)
                row.update_item = item
                self.updates_list4.append(row)
        def _refresh_dashboard(self):
            installed = len(self.packages_cache)
            updates = len(self.updates_data4)
            repos = len(self.repo_data4)
            if hasattr(self, "dash_summary4"):
                self.dash_summary4.set_text(
                    f"Installed packages loaded: {installed}\n"
                    f"Pending updates: {updates}\n"
                    f"Available repositories: {repos}\n"
                    "Tip: switch to Management to apply updates and explore repositories."
                )
            if hasattr(self, "updates_badge_btn4"):
                self.updates_badge_btn4.set_label(f"Updates ({updates})")
        def _refresh_install_list4(self):
            query = self.search_entry4.get_text().strip()
            idx = self.search_type4.get_selected()
            mode = GTK4_INSTALL_MODE_KEYS[idx if idx is not None else 0]
            if not query and mode == "unified":
                self._load_installed_packages()
                return
            self._search_packages()
        def _refresh_pkg_list_view4(self):
            if not hasattr(self, "pkg_store4"):
                return
            self.pkg_store4.remove_all()
            q = (self.pkg_filter_entry4.get_text().strip().lower() if hasattr(self, "pkg_filter_entry4") else "")
            installed_only = self.pkg_installed_check4.get_active() if hasattr(self, "pkg_installed_check4") else False
            max_rows = 5000
            shown = 0
            for item in self.packages_cache:
                repo_name = item.get("repo") or item.get("repository") or "unknown"
                pkg_name = item.get("name") or item.get("pkg") or "-"
                hay = (
                    f"{pkg_name} "
                    f"{item.get('pkg','')} "
                    f"{repo_name} "
                    f"{item.get('description','')} "
                    f"{item.get('version','')}"
                ).lower()
                if q and q not in hay:
                    continue
                if installed_only and not item.get("installed", False):
                    continue
                if shown >= max_rows:
                    break
                self.pkg_store4.append(
                    PackageRow4(
                        name=pkg_name,
                        version=item.get("version", "-"),
                        repository=repo_name,
                        installed=item.get("installed", False),
                        description=item.get("description", "") or "-",
                        raw_item=item,
                    )
                )
                shown += 1
            total = len(self.packages_cache)
            extra = " (capped at 5000)" if shown < total and (q or installed_only or total > max_rows) and shown >= max_rows else ""
            if hasattr(self, "install_stats_label4"):
                self.install_stats_label4.set_text(f"Results: {shown}/{total}{extra}")
        def _remove_appimage4(self):
            item = getattr(self, "selected_appimage4", None)
            if not item:
                self._append_output("Select an AppImage first.")
                return
            p = self._appimage_path4(item)
            if not p:
                self._append_output("Invalid AppImage path.")
                return
            if not self._confirm4("Remove AppImage", f"Delete file?\n{p}"):
                return
            try:
                os.remove(p)
                self._append_output(f"Removed AppImage: {p}")
                self.selected_appimage4 = None
                self._load_appimages4()
            except Exception as e:
                self._append_output(f"Remove AppImage failed: {e}")
        def _render_repo_pkg_list4(self):
            self._refresh_repo_pkg_list4()

        def _repo_add_to_batch4(self):
            if not self.selected_repo_pkg4:
                self._append_output("Select a package row first, then Add to Batch.")
                return
            self.selected_repo_pkg_names4.add(self.selected_repo_pkg4)
            self._append_output(f"Added to batch: {self.selected_repo_pkg4} ({len(self.selected_repo_pkg_names4)} total)")
            self._repo_update_batch_stats4()
        def _repo_batch_install4(self):
            names = sorted(self.selected_repo_pkg_names4)
            if not names:
                self._append_output("Batch is empty. Select packages and use Add to Batch.")
                return
            preview = ", ".join(names[:10]) + ("…" if len(names) > 10 else "")
            if not self._confirm4("Install batch", f"Install {len(names)} package(s)?\n{preview}"):
                return
            quoted = " ".join(shlex.quote(n) for n in names)
            self._run_stream_command(
                ["bash", "-lc", f"pacman -S --noconfirm {quoted}"],
                f"install {len(names)} packages",
                requires_privilege=True,
            )
        def _repo_batch_remove4(self):
            names = sorted(self.selected_repo_pkg_names4)
            if not names:
                if self.selected_repo_pkg4:
                    names = [self.selected_repo_pkg4]
                else:
                    self._append_output("Select an installed package to remove.")
                    return
            if not self._confirm4("Remove packages", f"Remove {len(names)} package(s)?"):
                return
            quoted = " ".join(shlex.quote(n) for n in names)
            self._run_stream_command(
                ["bash", "-lc", f"pacman -Rns --noconfirm {quoted}"],
                f"remove {len(names)} packages",
                requires_privilege=True,
            )
        def _repo_clear_batch4(self):
            self.selected_repo_pkg_names4.clear()
            self._append_output("Batch list cleared.")
            self._repo_update_batch_stats4()
        def _repo_export_selected4(self):
            if not self.selected_repo4:
                self._append_output("Select a repository first.")
                return
            filename = f"pkger-{self.selected_repo4}-packages.txt"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    for it in self.repo_data4.get(self.selected_repo4, []):
                        name = it["name"] if isinstance(it, dict) else str(it)
                        f.write(f"{name}\n")
                self._append_output(f"Exported repository list to {filename}")
                self._set_status(f"Exported {self.selected_repo4} package list")
            except Exception as e:
                self._append_output(f"Export failed: {e}")
        def _repo_install_selected4(self):
            if not self.selected_repo_pkg4:
                self._append_output("Select a package from the list first.")
                return
            name = self.selected_repo_pkg4
            if not self._confirm4("Install package", f"Install '{name}' from {self.selected_repo4}?"):
                return
            self._run_stream_command(
                ["bash", "-lc", f"pacman -S --noconfirm {shlex.quote(name)}"],
                f"install {name}",
                requires_privilege=True,
            )
        def _repo_remove_selected4(self):
            if not self.selected_repo_pkg4:
                self._append_output("Select an installed package first.")
                return
            name = self.selected_repo_pkg4
            if not self._confirm4("Remove package", f"Remove '{name}'?"):
                return
            self._run_stream_command(
                ["bash", "-lc", f"pacman -Rns --noconfirm {shlex.quote(name)}"],
                f"remove {name}",
                requires_privilege=True,
            )
        def _repo_set_enabled4(self, enable):
            if not self.selected_repo4:
                self._append_output("Select a repository first.")
                return
            repo = self.selected_repo4
            self._append_output(
                f"To {'enable' if enable else 'disable'} '{repo}', edit /etc/pacman.conf "
                f"or remove the repo file under /etc/pacman.d/."
            )
        def _repo_update_batch_stats4(self):
            if hasattr(self, "repo_pkg_stats_label4") and self.selected_repo4:
                base = f"{self.selected_repo4}: {len(self.repo_pkg_results4 or [])} result(s)"
                if self.selected_repo_pkg_names4:
                    base += f"  ·  batch: {len(self.selected_repo_pkg_names4)}"
                n = sum(1 for i in (self.repo_pkg_results4 or []) if i.get("installed"))
                if n:
                    base += f"  ·  {n} installed in list"
                self.repo_pkg_stats_label4.set_text(base)
        def _request_privilege4(self):
            if os.geteuid() == 0:
                return True
            if PRIV_TOOL == "doas":
                self._append_output("Using doas for privilege escalation.")
                return True
            if PRIV_TOOL != "sudo":
                self._append_output("No authentication tool found (sudo/doas).")
                return False

            dialog = Gtk4.Dialog(title="Authentication Required", transient_for=self, modal=True)
            dialog.add_button("Cancel", Gtk4.ResponseType.CANCEL)
            dialog.add_button("OK", Gtk4.ResponseType.OK)
            content = dialog.get_content_area()
            content.set_spacing(8)
            content.append(Gtk4.Label(label="Enter your sudo password:"))
            entry = Gtk4.PasswordEntry()
            entry.set_show_peek_icon(True)
            content.append(entry)

            result = {"done": False, "ok": False, "password": ""}

            def on_resp(_dlg, response):
                if response == Gtk4.ResponseType.OK:
                    result["ok"] = True
                    result["password"] = entry.get_text()
                result["done"] = True
                dialog.hide()

            dialog.connect("response", on_resp)
            dialog.present()
            while not result["done"]:
                while GLib.MainContext.default().iteration(False):
                    pass
                time.sleep(0.01)
            dialog.destroy()
            if not result["ok"] or not result["password"]:
                return False
            pwd = result["password"]
            try:
                chk = subprocess.run(
                    ["sudo", "-S", "-k", "true"],
                    input=pwd + "\n",
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                if chk.returncode != 0:
                    self._append_output("Authentication failed: incorrect sudo password.")
                    return False
            except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
                log.debug("GTK4 sudo probe failed: %s", exc, exc_info=True)
                self._append_output("Authentication check failed.")
                return False
            self.sudo_password4 = pwd
            return True
        def _run_index_action4(self, action):
            if action in ("index", "install", "updates", "repos", "appimages", "system"):
                self.mode_stack4.set_visible_child_name(action)
                return
            if action == "act_upgrade":
                self._run_system_action("update"); return
            if action == "act_load_updates":
                self._load_updates(); return
            if action == "act_load_repos":
                self._load_repos(); return
            if action == "act_load_appimages":
                self._load_appimages4(); return
        def _run_pkg_action(self, action):
            if not self.current_selection:
                self._append_output("Select a package first.")
                return
            sel = self.current_selection
            name = sel.get("name") or sel.get("pkg") or ""
            if not name:
                self._append_output("Invalid selection (missing package name).")
                return
            repo = str(sel.get("repository") or sel.get("repo") or "").lower()
            qname = shlex.quote(name)

            if action == "remove":
                if repo == "flatpak" and command_exists("flatpak"):
                    cmd = ["bash", "-lc", f"flatpak uninstall -y {qname}"]
                    privileged = False
                else:
                    cmd = ["bash", "-lc", f"pacman -Rns --noconfirm {qname}"]
                    privileged = True
            elif repo == "flatpak" and command_exists("flatpak"):
                cmd = ["bash", "-lc", f"flatpak install -y {qname}"]
                privileged = False
            elif repo == "aur":
                if not AUR_HELPER:
                    self._append_output("No AUR helper installed (yay or paru). Install one to manage AUR packages.")
                    return
                cmd = ["bash", "-lc", f"{shlex.quote(AUR_HELPER)} -S --noconfirm {qname}"]
                privileged = False
            else:
                cmd = ["bash", "-lc", f"pacman -S --noconfirm {qname}"]
                privileged = True

            if privileged:
                self._append_output("Note: this operation uses administrator privileges when not root.")
            else:
                self._append_output("Note: the AUR helper handles privilege escalation when needed (do not run it as root).")
            self._run_stream_command(cmd, f"{action} {name}", requires_privilege=privileged)
        def _run_scheduled_repo_pkg_search4(self):
            self._repo_pkg_search_timer4 = 0
            q = self.repo_pkg_search_entry4.get_text().strip() if hasattr(self, "repo_pkg_search_entry4") else ""
            if len(q) >= 2:
                self._search_repo_packages4()
            return False
        def _run_scheduled_search4(self):
            self.search_timer_id4 = 0
            self._search_packages()
            return False
        def _run_stream_command(self, cmd, label, requires_privilege=False):
            self._set_status(f"Running {label}...")
            self._set_progress(8)
            self.command_history4.append(
                {"time": datetime.now().strftime("%H:%M:%S"), "label": label, "cmd": " ".join(cmd)}
            )
            if len(self.command_history4) > 200:
                self.command_history4 = self.command_history4[-200:]

            run_cmd = cmd
            password = None
            if requires_privilege and os.geteuid() != 0:
                if not self._request_privilege4():
                    self._append_output("Authentication cancelled.")
                    self._set_status("Ready")
                    self._set_progress(0)
                    return
                if PRIV_TOOL == "sudo":
                    password = self.sudo_password4
                    if len(run_cmd) >= 3 and run_cmd[0] == "bash" and run_cmd[1] == "-lc":
                        if not run_cmd[2].strip().startswith("sudo "):
                            run_cmd = ["bash", "-lc", f"sudo {run_cmd[2]}"]
                    elif run_cmd and run_cmd[0] != "sudo":
                        run_cmd = ["sudo"] + run_cmd
                elif PRIV_TOOL == "doas":
                    if len(run_cmd) >= 3 and run_cmd[0] == "bash" and run_cmd[1] == "-lc":
                        if not run_cmd[2].strip().startswith("doas "):
                            run_cmd = ["bash", "-lc", f"doas {run_cmd[2]}"]
                    elif run_cmd and run_cmd[0] != "doas":
                        run_cmd = ["doas"] + run_cmd

            def on_progress(line):
                ll = line.lower()
                if "downloading" in ll:
                    GLib.idle_add(self._set_progress, 45)
                elif "installing" in ll or "removing" in ll:
                    GLib.idle_add(self._set_progress, 75)

            def worker():
                ret = run_command_stream(
                    run_cmd,
                    lambda t: GLib.idle_add(self._append_output, t),
                    on_progress_hint=on_progress,
                    password=password,
                )
                GLib.idle_add(self._set_progress, 100)
                GLib.idle_add(self._set_status, f"{label} finished (exit {ret})")
                time.sleep(0.2)
                GLib.idle_add(self._set_progress, 0)

            threading.Thread(target=worker, daemon=True).start()
        def _run_system_action(self, action):
            """Shortcuts from nav / Install tab — same confirmations as System workspace."""
            route = {
                "update": "update",
                "clean": "clean",
                "sync": "sync",
                "fixdeps": "fixdeps",
                "paccache": "paccache",
            }
            if action == "mirrors":
                self._gtk4_reflector()
            elif action in route:
                self._gtk4_maybe_run_stream(route[action])
            else:
                self._append_output(f"Unknown system action: {action}")
        def _schedule_repo_pkg_search4(self):
            if self._repo_pkg_search_timer4:
                try:
                    GLib.source_remove(self._repo_pkg_search_timer4)
                except Exception:
                    pass
                self._repo_pkg_search_timer4 = 0
            self._repo_pkg_search_timer4 = GLib.timeout_add(350, self._run_scheduled_repo_pkg_search4)
            return False
        def _schedule_search4(self):
            if self.search_timer_id4:
                try:
                    GLib.source_remove(self.search_timer_id4)
                except Exception:
                    pass
                self.search_timer_id4 = 0
            self.search_timer_id4 = GLib.timeout_add(220, self._run_scheduled_search4)
            return False
        def _search_packages(self):
            query = self.search_entry4.get_text().strip()
            idx = self.search_type4.get_selected()
            mode = GTK4_INSTALL_MODE_KEYS[idx if idx is not None else 0]

            if mode == "developer" and not query:
                self._append_output("Developer (packager): enter a substring (install expac for repo-wide match; paru adds AUR maintainer search).")
                self._set_status("Enter developer search text")
                return
            if mode == "installed" and not query:
                self._append_output("Installed (search): enter text for pacman -Qs, or pick Installed (native/foreign/Flatpak/AppImage).")
                self._set_status("Enter installed search text")
                return
            if mode in ("unified", "official", "aur", "flatpak") and not query:
                self._append_output("Enter a query, pick Installed (…), or press ↻ on Unified with empty query to load pacman -Q.")
                self._set_status("Missing query")
                return

            self._set_status(f"Searching ({mode})…")
            self.search_generation += 1
            current_generation = self.search_generation

            def on_search_results(pkgs, generation):
                if generation != self.search_generation:
                    # Stale results, ignore
                    return
                self._populate_pkg_list(pkgs)
                self._set_status(f"Found {len(pkgs)} packages")
                self._set_progress(0)

            if hasattr(self, "_current_search_worker") and self._current_search_worker:
                self._current_search_worker.cancel()

            worker = SearchWorker(query, mode, on_search_results, current_generation)
            self._current_search_worker = worker
            worker.start()
            self._set_progress(10)
        def _search_repo_packages4(self, installed_only_mode=False):
            if not self.selected_repo4:
                self._append_output("Select a repository first.")
                return
            repo = self.selected_repo4
            query = ""
            if hasattr(self, "repo_pkg_search_entry4"):
                query = self.repo_pkg_search_entry4.get_text().strip()
            if installed_only_mode:
                if hasattr(self, "repo_installed_only_check4"):
                    self.repo_installed_only_check4.set_active(True)
                self._refresh_repo_pkg_list4()
                return
            if not query:
                self._append_output("Enter at least 2 characters to search, or use Installed only.")
                return
            self._repo_pkg_search_generation4 += 1
            generation = self._repo_pkg_search_generation4
            self._set_status(f"Searching {repo}…")

            def worker():
                pkgs = fetch_packages_in_repo(repo, query, installed_only=False, timeout=150)

                def apply():
                    if generation != self._repo_pkg_search_generation4:
                        return
                    names = {p.get("name") for p in pkgs if p.get("name")}
                    filtered = [
                        it for it in self.repo_data4.get(repo, [])
                        if (it.get("name") if isinstance(it, dict) else str(it)) in names
                    ]
                    if not filtered and pkgs:
                        filtered = pkgs
                    self.repo_pkg_results4 = filtered
                    self._refresh_repo_pkg_list4()
                    self._set_status(f"Found {len(filtered)} package(s) in {repo}")

                GLib.idle_add(apply)

            threading.Thread(target=worker, daemon=True).start()
        def _set_progress(self, value, text=""):
            def apply():
                frac = max(0.0, min(1.0, float(value) / 100.0 if value > 1 else value))
                self.progress_bar.set_fraction(frac)
                if text:
                    self.progress_bar.set_text(text)
            GLib.idle_add(apply)
        def _set_status(self, text):
            GLib.idle_add(self.status_lbl.set_label, text)
        def _show_add_repository_dialog4(self):
            dlg = Gtk4.Dialog(
                title="Add Repository",
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
            )
            dlg.set_default_size(580, 520)
            dlg.add_button("Cancel", Gtk4.ResponseType.CANCEL)
            dlg.add_button("Add Repository", Gtk4.ResponseType.OK)
            dlg.set_default_response(Gtk4.ResponseType.OK)

            content = dlg.get_content_area()
            content.set_spacing(10)
            content.set_margin_top(12)
            content.set_margin_bottom(8)
            content.set_margin_start(12)
            content.set_margin_end(12)

            intro = Gtk4.Label(
                label="Add a pacman repository (pacman.conf section or /etc/pacman.d include). "
                "Administrator privileges are required."
            )
            intro.set_wrap(True)
            intro.set_xalign(0)
            intro.add_css_class("dim-label")
            content.append(intro)

            stack = Gtk4.Stack()
            stack.set_transition_type(Gtk4.StackTransitionType.SLIDE_LEFT_RIGHT)
            stack.set_vexpand(True)

            # --- Presets ---
            presets_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            presets_box.set_margin_top(4)
            presets_scroll = Gtk4.ScrolledWindow()
            presets_scroll.set_policy(Gtk4.PolicyType.NEVER, Gtk4.PolicyType.AUTOMATIC)
            presets_scroll.set_min_content_height(200)
            presets_list = Gtk4.ListBox()
            presets_list.set_selection_mode(Gtk4.SelectionMode.SINGLE)
            dlg._pkger_preset_cmd = None
            for preset in get_repository_presets():
                row = Gtk4.ListBoxRow()
                v = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=2)
                t = Gtk4.Label(label=preset["title"])
                t.set_xalign(0)
                t.add_css_class("heading")
                d = Gtk4.Label(label=preset.get("description", ""))
                d.set_xalign(0)
                d.set_wrap(True)
                d.add_css_class("dim-label")
                v.append(t)
                v.append(d)
                row.set_child(v)
                row.preset_cmd = preset["cmd"]
                row.preset_title = preset["title"]
                presets_list.append(row)

            def on_preset_selected(_lb, row):
                if row is not None:
                    dlg._pkger_preset_cmd = getattr(row, "preset_cmd", None)
                    dlg._pkger_preset_title = getattr(row, "preset_title", "preset")

            presets_list.connect("row-selected", on_preset_selected)
            presets_scroll.set_child(presets_list)
            presets_box.append(presets_scroll)
            stack.add_titled(presets_box, "presets", "Presets")

            # --- URL ---
            url_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            url_hint = Gtk4.Label(
                label="Paste a mirrorlist or include file URL (HTTPS). It will be saved under /etc/pacman.d/."
            )
            url_hint.set_xalign(0)
            url_hint.set_wrap(True)
            url_box.append(url_hint)
            url_entry = Gtk4.Entry()
            url_entry.set_placeholder_text("https://example.com/custom-mirrorlist")
            url_box.append(url_entry)
            url_id = Gtk4.Entry()
            url_id.set_placeholder_text("Optional repository ID override")
            url_box.append(url_id)
            stack.add_titled(url_box, "url", "From URL")

            # --- Local file ---
            file_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            file_hint = Gtk4.Label(
                label="Select a pacman include/mirrorlist file from disk (installed into /etc/pacman.d/)."
            )
            file_hint.set_xalign(0)
            file_hint.set_wrap(True)
            file_box.append(file_hint)
            file_path_lbl = Gtk4.Label(label="No file selected", xalign=0)
            file_path_lbl.add_css_class("dim-label")
            file_box.append(file_path_lbl)
            dlg._pkger_repo_file_path = None

            def on_pick_repo_file(_btn):
                def on_path(path):
                    dlg._pkger_repo_file_path = path
                    file_path_lbl.set_text(path or "No file selected")

                gtk4_run_open_file(self, "Select pacman include file", ["*"], on_path)

            pick_btn = Gtk4.Button(label="Choose file…")
            pick_btn.connect("clicked", on_pick_repo_file)
            file_box.append(pick_btn)
            stack.add_titled(file_box, "file", "From File")

            # --- AUR install ---
            aur_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            aur_hint = Gtk4.Label(
                label="Install an AUR package by name (requires yay or paru)."
            )
            aur_hint.set_xalign(0)
            aur_hint.set_wrap(True)
            if not AUR_HELPER:
                aur_hint.set_label(aur_hint.get_text() + "\n(Install yay or paru to use AUR.)")
            aur_box.append(aur_hint)
            aur_entry = Gtk4.Entry()
            aur_entry.set_placeholder_text("e.g. visual-studio-code-bin")
            aur_box.append(aur_entry)
            stack.add_titled(aur_box, "aur", "AUR")

            # --- Manual ---
            manual_box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            manual_hint = Gtk4.Label(
                label="Create a custom [repo] section in /etc/pacman.conf (Server URL required)."
            )
            manual_hint.set_xalign(0)
            manual_hint.set_wrap(True)
            manual_box.append(manual_hint)
            manual_id = Gtk4.Entry()
            manual_id.set_placeholder_text("Repository ID (e.g. my-custom-repo)")
            manual_name = Gtk4.Entry()
            manual_name.set_placeholder_text("Display name")
            manual_baseurl = Gtk4.Entry()
            manual_baseurl.set_placeholder_text("https://mirror.example.com/$repo/os/$arch")
            manual_gpgkey = Gtk4.Entry()
            manual_gpgkey.set_placeholder_text("gpgkey= (optional)")
            manual_gpgcheck = Gtk4.CheckButton(label="GPG check")
            manual_gpgcheck.set_active(True)
            manual_enabled = Gtk4.CheckButton(label="Enabled")
            manual_enabled.set_active(True)
            for w in (manual_id, manual_name, manual_baseurl, manual_gpgkey):
                manual_box.append(w)
            manual_box.append(manual_gpgcheck)
            manual_box.append(manual_enabled)
            stack.add_titled(manual_box, "manual", "Manual")

            switcher = Gtk4.StackSwitcher()
            switcher.set_stack(stack)
            switcher.set_halign(Gtk4.Align.CENTER)
            content.append(switcher)
            content.append(stack)

            def build_command():
                page = stack.get_visible_child_name()
                if page == "presets":
                    if not getattr(dlg, "_pkger_preset_cmd", None):
                        return None, "Select a preset from the list."
                    return dlg._pkger_preset_cmd, getattr(dlg, "_pkger_preset_title", "preset")
                if page == "url":
                    cmd = build_addrepo_from_url_command(
                        url_entry.get_text().strip(),
                        url_id.get_text().strip() or None,
                    )
                    if not cmd:
                        return None, "Enter a valid repository URL."
                    return cmd, "add repository from URL"
                if page == "file":
                    path = getattr(dlg, "_pkger_repo_file_path", None)
                    if not path:
                        return None, "Choose a file first."
                    cmd = build_addrepo_from_url_command(path)
                    return cmd, f"add repository from {path}"
                if page == "aur":
                    spec = aur_entry.get_text().strip()
                    cmd = build_aur_install_command(spec)
                    if not cmd:
                        return None, "Enter an AUR package name."
                    return cmd, f"install AUR {spec}"
                if page == "manual":
                    cmd = build_addrepo_manual_command(
                        manual_id.get_text().strip(),
                        manual_name.get_text().strip(),
                        manual_baseurl.get_text().strip(),
                        gpgcheck=manual_gpgcheck.get_active(),
                        gpgkey=manual_gpgkey.get_text().strip(),
                        enabled=manual_enabled.get_active(),
                    )
                    if not cmd:
                        return None, "Repository ID and Server URL are required."
                    return cmd, f"add repository {manual_id.get_text().strip()}"
                return None, "Unknown tab."

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.OK:
                    return
                cmd, label = build_command()
                if not cmd:
                    self._append_output(label or "Could not build repository command.")
                    return
                preview = " ".join(shlex.quote(c) for c in cmd)
                if not self._confirm4(
                    "Add repository",
                    f"Run this command?\n\n{preview}",
                ):
                    return
                self._append_output(f"Adding repository: {label}")
                run_cmd = cmd if cmd and cmd[0] == "bash" else ["bash", "-lc", preview]
                self._run_stream_command(
                    run_cmd,
                    label,
                    requires_privilege=True,
                )
                GLib.timeout_add(2000, self._load_repos)

            dlg.connect("response", on_response)
            dlg.present()
        def _show_repo_installed4(self):
            if not self.selected_repo4:
                self._append_output("Select a repository first.")
                return
            if hasattr(self, "repo_pkg_search_entry4"):
                self.repo_pkg_search_entry4.set_text("")
            self._search_repo_packages4(installed_only_mode=True)
        def _sort_packages_cache4(self):
            key = self.pkg_sort_key4 or "name"
            asc = self.pkg_sort_asc4

            def sort_val(it):
                if key == "installed":
                    return (0 if it.get("installed") else 1, str(it.get("name", "")).lower())
                if key == "version":
                    return str(it.get("version", "")).lower()
                if key == "repository":
                    return str(it.get("repository") or it.get("repo", "")).lower()
                if key == "description":
                    return str(it.get("description", "")).lower()
                return str(it.get("name") or it.get("pkg", "")).lower()

            try:
                self.packages_cache.sort(key=sort_val, reverse=not asc)
            except Exception:
                pass
        def _toggle_fullscreen4(self):
            self._toggle_fullscreen()
        def _updates_add_section_header4(self, title):
            row = Gtk4.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            lbl = Gtk4.Label(label=title)
            lbl.set_xalign(0)
            lbl.add_css_class("heading")
            row.set_child(lbl)
            self.updates_list4.append(row)
        def _updates_filter_match4(self, item, query):
            if not query:
                return True
            hay = " ".join(
                str(item.get(k, ""))
                for k in (
                    "name",
                    "arch",
                    "from",
                    "to",
                    "repository",
                    "source",
                    "severity",
                    "kind",
                    "advisory",
                )
            ).lower()
            return query in hay
        def _updates_select_security4(self):
            any_sec = False
            for item in self.updates_data4:
                is_sec = _is_security_update(item)
                item["selected"] = is_sec
                any_sec = any_sec or is_sec
            if not any_sec:
                self._append_output("No security-classified updates found in the current list.")
            self._populate_updates_list()
        def _updates_set_selection4(self, selected):
            for item in self.updates_data4:
                item["selected"] = bool(selected)
            self._populate_updates_list()



class PkgerApp(Adw.Application if Adw else Gtk4.Application):
    def __init__(self):
        super().__init__(application_id="com.almezali.pkger.gtk4")

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = PkgerWindow(application=self)
        win.present()
        if hasattr(win, "_apply_install_pane_ratio4"):
            GLib.idle_add(win._apply_install_pane_ratio4)
            GLib.timeout_add(150, win._apply_install_pane_ratio4)


def main():
    _configure_debug_logging()

    parser = argparse.ArgumentParser(
        prog="pkger",
        description=(
            "PKGER — graphical package manager for Arch Linux. "
            "Launch with no arguments to open the main window."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {__version__}",
        help="Print version and exit.",
    )
    _args, _unknown_argv = parser.parse_known_args()

    if not command_exists("pacman"):
        print("Warning: pacman not found in PATH. Some features will not work.", file=sys.stderr)

    app = PkgerApp()
    try:
        app.run(None)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
