#!/usr/bin/env python3
"""
PKGER — GTK 3 package manager for Arch Linux and Arch-based distributions.

Sidebar package list with icons, HPaned layout, notebook (Operations, Details,
Repositories, Updates, System), pacman/yay/paru integration, and maintenance tools.
"""

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

def _patch_pygobject_glib_unix_signal_deprecation():
    """PyGObject's gi.overrides.GLib can call deprecated_attr('GLib', 'unix_signal_add_full', ...)
    when GLibUnix.signal_add exists but the introspection module has no unix_signal_add (e.g. some
    Nuitka/embed builds). load_overrides then raises AssertionError. Skip that orphan deprecation."""
    import gi.overrides as go

    _orig = go.deprecated_attr

    def _deprecated_attr(namespace, attr, replacement):
        if namespace == "GLib" and attr == "unix_signal_add_full":
            from gi.module import get_introspection_module

            g = get_introspection_module("GLib")
            if not hasattr(g, "unix_signal_add"):
                return
        return _orig(namespace, attr, replacement)

    go.deprecated_attr = _deprecated_attr


def _shim_glib_introspection_unix_signal_aliases():
    """Expose unix_signal_add_full on the raw introspection module when only unix_signal_add exists."""
    from gi.module import get_introspection_module

    g = get_introspection_module("GLib")
    if hasattr(g, "unix_signal_add") and not hasattr(g, "unix_signal_add_full"):
        g.unix_signal_add_full = g.unix_signal_add


try:
    import gi

    gi.require_version("GLib", "2.0")
    gi.require_version("Gtk", "3.0")
    _patch_pygobject_glib_unix_signal_deprecation()
    _shim_glib_introspection_unix_signal_aliases()
    from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Pango
except Exception as e:
    print("=== GTK Import Error ===\n")
    import traceback

    traceback.print_exc()
    print(
        "\nPyGObject (GTK) is required. On Arch: pacman -S python-gobject gtk3\n"
        "If you build with Nuitka, prefer Python 3.13 and a matching pygobject; "
        "see also: https://nuitka.net/info/gi.html\n"
    )
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass
    sys.exit(1)

APP_NAME = "PKGER"
APP_VERSION = "v0.2.1"
APP_DEVELOPER = "almezali"
APP_YEAR = "2026"

# List icon size (pixels) for sidebar, Repositories, and Updates — compact for the UI.
PACKAGE_LIST_ICON_SIZE = 14

# System tray / status area (same icon name as window icon).
TRAY_ICON_NAME = "package-x-generic"

ARCH_NEWS_RSS = "https://archlinux.org/feeds/news/"


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
# Package icons
# ----------------------------

class PackageIconManager:
    """Resolve package row icons from the GTK icon theme, .desktop files, and common paths."""

    def __init__(self):
        self.icon_cache = {}
        self.icon_paths = [
            "/usr/share/applications",
            "/usr/share/pixmaps",
            "/usr/share/icons/hicolor/128x128/apps",
            "/usr/share/icons/hicolor/64x64/apps",
            "/usr/share/icons/hicolor/48x48/apps",
            "/usr/share/icons/hicolor/32x32/apps",
            "/usr/share/icons/hicolor/16x16/apps",
            "/usr/share/icons/Adwaita/128x128/apps",
            "/usr/share/icons/Adwaita/64x64/apps",
            "/usr/share/icons/Adwaita/48x48/apps",
            "/usr/share/icons/Adwaita/32x32/apps",
            "/usr/share/icons/Adwaita/16x16/apps",
            os.path.expanduser("~/.local/share/applications"),
            os.path.expanduser("~/.local/share/icons"),
            "/var/lib/flatpak/exports/share/applications",
            "/usr/share/flatpak/exports/share/applications",
        ]
        self.default_icons = {
            "system": "system-run-symbolic",
            "development": "applications-development-symbolic",
            "multimedia": "applications-multimedia-symbolic",
            "office": "applications-office-symbolic",
            "games": "applications-games-symbolic",
            "network": "applications-internet-symbolic",
            "graphics": "applications-graphics-symbolic",
            "education": "applications-education-symbolic",
            "science": "applications-science-symbolic",
            "utility": "applications-utilities-symbolic",
        }
        self.category_mapping = self._load_category_mapping()

    def _load_category_mapping(self):
        mapping = {}
        try:
            result = subprocess.run(["pacman", "-Si"], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                current_pkg = None
                for line in result.stdout.split("\n"):
                    if line.startswith("Name"):
                        current_pkg = line.split(":", 1)[1].strip()
                    elif line.startswith("Groups") and current_pkg:
                        groups = line.split(":", 1)[1].strip()
                        if groups:
                            mapping[current_pkg] = groups.split()
        except Exception:
            pass
        mapping.update(
            {
                "firefox": ["network"],
                "chromium": ["network"],
                "vlc": ["multimedia"],
                "gimp": ["graphics"],
                "libreoffice": ["office"],
                "steam": ["games"],
                "git": ["development"],
                "docker": ["development"],
                "linux": ["system"],
                "systemd": ["system"],
            }
        )
        return mapping

    def get_package_icon(self, package_name, size=PACKAGE_LIST_ICON_SIZE):
        ck = (package_name, size)
        if ck in self.icon_cache:
            return self.icon_cache[ck]
        icon = self._quick_icon_search(package_name, size)
        if icon:
            self.icon_cache[ck] = icon
            return icon
        category = self._get_package_category(package_name)
        icon = self._get_category_icon(category, size)
        if icon:
            self.icon_cache[ck] = icon
            return icon
        fallback = self._get_fallback_icon(size)
        self.icon_cache[ck] = fallback
        return fallback

    def get_better_fallback_icon(self, package_name, size=PACKAGE_LIST_ICON_SIZE):
        icon = self._find_package_icon_enhanced(package_name, size)
        if icon:
            return icon
        name_lower = package_name.lower()
        if any(x in name_lower for x in ("dev", "devel", "lib", "gcc", "clang", "python", "git", "cmake")):
            return self._load_icon_by_name("applications-development-symbolic", size)
        if any(x in name_lower for x in ("video", "audio", "vlc", "ffmpeg", "pulse", "alsa")):
            return self._load_icon_by_name("applications-multimedia-symbolic", size)
        if any(x in name_lower for x in ("gimp", "inkscape", "blender", "krita")):
            return self._load_icon_by_name("applications-graphics-symbolic", size)
        if any(x in name_lower for x in ("office", "libreoffice", "document")):
            return self._load_icon_by_name("applications-office-symbolic", size)
        if any(x in name_lower for x in ("game", "steam", "wine")):
            return self._load_icon_by_name("applications-games-symbolic", size)
        if any(x in name_lower for x in ("browser", "http", "ssh", "network")):
            return self._load_icon_by_name("applications-internet-symbolic", size)
        return self._load_icon_by_name("package-x-generic", size)

    def _quick_icon_search(self, package_name, size):
        for icon_name in (
            package_name,
            package_name.lower(),
            f"{package_name}-symbolic",
            f"{package_name.lower()}-symbolic",
        ):
            icon = self._load_icon_by_name(icon_name, size)
            if icon:
                return icon
        desktop_icon = self._find_desktop_icon_simple(package_name)
        if desktop_icon:
            return self._load_icon_by_name(desktop_icon, size)
        return None

    def _find_desktop_icon_simple(self, package_name):
        for path in ("/usr/share/applications", os.path.expanduser("~/.local/share/applications")):
            if not os.path.isdir(path):
                continue
            desktop_file = os.path.join(path, f"{package_name}.desktop")
            if os.path.isfile(desktop_file):
                try:
                    with open(desktop_file, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("Icon="):
                                n = line.split("=", 1)[1].strip()
                                if n:
                                    return n
                except OSError:
                    continue
        return None

    def _find_package_icon_enhanced(self, package_name, size):
        desktop_icon = self._find_desktop_file_icon(package_name)
        if desktop_icon:
            return self._load_icon_by_name(desktop_icon, size)
        icon_file = self._find_icon_file(package_name)
        if icon_file:
            return self._load_icon_from_path(icon_file, size)
        for icon_name in (
            package_name,
            package_name.lower(),
            f"{package_name}-symbolic",
            f"org.{package_name}.{package_name}",
        ):
            icon = self._load_icon_by_name(icon_name, size)
            if icon:
                return icon
        return None

    def _find_desktop_file_icon(self, package_name):
        for path in (
            "/usr/share/applications",
            os.path.expanduser("~/.local/share/applications"),
            "/var/lib/flatpak/exports/share/applications",
        ):
            if not os.path.isdir(path):
                continue
            try:
                for file in os.listdir(path):
                    if not file.endswith(".desktop"):
                        continue
                    if package_name.lower() not in file.lower():
                        continue
                    try:
                        with open(os.path.join(path, file), "r", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("Icon="):
                                    n = line.split("=", 1)[1].strip()
                                    if n:
                                        return n
                    except OSError:
                        continue
            except OSError:
                continue
        return None

    def _find_icon_file(self, package_name):
        exts = (".png", ".svg", ".xpm", ".ico")
        for path in self.icon_paths:
            if not os.path.isdir(path):
                continue
            for ext in exts:
                p = os.path.join(path, f"{package_name}{ext}")
                if os.path.isfile(p):
                    return p
            try:
                for file in os.listdir(path):
                    if package_name.lower() in file.lower() and file.endswith(exts):
                        return os.path.join(path, file)
            except OSError:
                continue
        return None

    def _get_package_category(self, package_name):
        if package_name in self.category_mapping:
            g = self.category_mapping[package_name]
            return g[0] if g else "utility"
        nl = package_name.lower()
        if any(x in nl for x in ("dev", "lib", "gcc", "python", "git", "cmake")):
            return "development"
        if any(x in nl for x in ("video", "audio", "vlc", "ffmpeg")):
            return "multimedia"
        if any(x in nl for x in ("gimp", "ink", "draw", "krita")):
            return "graphics"
        if any(x in nl for x in ("office", "doc", "sheet")):
            return "office"
        if any(x in nl for x in ("game", "steam")):
            return "games"
        if any(x in nl for x in ("http", "ssh", "vpn", "net")):
            return "network"
        if any(x in nl for x in ("system", "kernel", "udev", "firmware")):
            return "system"
        return "utility"

    def _get_category_icon(self, category, size):
        icon_name = self.default_icons.get(category, "applications-utilities-symbolic")
        return self._load_icon_by_name(icon_name, size)

    def _get_fallback_icon(self, size):
        return self._load_icon_by_name("package-x-generic", size)

    def _load_icon_by_name(self, icon_name, size):
        try:
            theme = Gtk.IconTheme.get_default()
            info = theme.lookup_icon(icon_name, size, 0)
            if info:
                return info.load_icon()
        except Exception:
            pass
        return None

    def _load_icon_from_path(self, path, size):
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
        except Exception:
            return None


class PackageInfo:
    """Lightweight package record (optional structured row)."""

    def __init__(self, data):
        self.name = data.get("name", "")
        self.version = data.get("version", "")
        self.description = data.get("description", "")
        self.repository = data.get("repository", "")
        self.installed = data.get("installed", False)
        self.license = data.get("license", "")
        self.url = data.get("url", "")


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
        if password and command and command[0] != "sudo":
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
    except Exception:
        pass
    return None


_REPO_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_.\[\]]{0,127}$")


def _is_plausible_repo_name(token):
    """Fallback when pacman-conf is unavailable."""
    if not token or not _REPO_TOKEN_RE.match(token):
        return False
    if token[0].isdigit():
        return False
    return True


# ----------------------------
# Worker threads
# ----------------------------

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


def _aur_helper_cmd():
    if command_exists("yay"):
        return "yay"
    if command_exists("paru"):
        return "paru"
    return None


class SearchWorker(threading.Thread):
    def __init__(self, query, search_type, on_results, icon_manager=None):
        super().__init__(daemon=True)
        self.query = query
        self.search_type = search_type  # 'official' | 'aur' | 'installed' | 'all'
        self.on_results = on_results
        self.icon_manager = icon_manager

    def _is_installed(self, name):
        try:
            r = subprocess.run(["pacman", "-Q", name], capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    def _parse_pacman_search(self, output):
        """Parse pacman -Ss output (repo/name, version, optional [installed] markers)."""
        packages = []
        current_package = None
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "/" in line and not line.startswith("    "):
                parts = line.split(" ", 1)
                if len(parts) >= 2:
                    repo_name = parts[0]
                    version_desc = parts[1]
                    if "/" in repo_name:
                        repo, name = repo_name.split("/", 1)
                    else:
                        repo = "unknown"
                        name = repo_name
                    version = version_desc
                    installed = False
                    if "[installed:" in version_desc:
                        version = version_desc.split(" [installed:", 1)[0]
                        installed = True
                    elif "[installed]" in version_desc:
                        version = version_desc.replace(" [installed]", "")
                        installed = True
                    elif not installed:
                        installed = self._is_installed(name)
                    current_package = {
                        "name": repo_name,
                        "pkg": name,
                        "version": version,
                        "description": "",
                        "repository": repo,
                        "installed": installed,
                    }
                    packages.append(current_package)
            elif line.startswith("    ") and current_package:
                description = line.strip()
                if description and not description.startswith("["):
                    current_package["description"] = description
        return packages

    def _parse_aur_search(self, output):
        """Parse yay/paru -Ss lines (votes in parentheses, optional [installed])."""
        packages = []
        current_package = None
        for line in output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "aur/" in line and not line.startswith("    "):
                parts = line.split(" ", 1)
                if len(parts) >= 2:
                    repo_name = parts[0]
                    version_desc = parts[1]
                    name = repo_name.replace("aur/", "")
                    version = version_desc
                    installed = False
                    if "(" in version and ")" in version:
                        version = version.split("(", 1)[0].strip()
                    if "[installed]" in version:
                        version = version.replace(" [installed]", "")
                        installed = True
                    current_package = {
                        "name": name,
                        "pkg": name,
                        "version": version,
                        "description": "",
                        "repository": "aur",
                        "installed": installed,
                    }
                    packages.append(current_package)
            elif line.startswith("    ") and current_package:
                description = line.strip()
                if description and not description.startswith("["):
                    current_package["description"] = description
        return packages

    def run(self):
        try:
            if self.search_type == "official":
                result = subprocess.run(
                    ["pacman", "-Ss", self.query], capture_output=True, text=True, timeout=30
                )
                pkgs = self._parse_pacman_search(result.stdout)
            elif self.search_type == "aur":
                helper = _aur_helper_cmd()
                if not helper:
                    pkgs = []
                else:
                    result = subprocess.run(
                        [helper, "-Ss", self.query], capture_output=True, text=True, timeout=30
                    )
                    pkgs = self._parse_aur_search(result.stdout)
            elif self.search_type == "all":
                pkgs = []
                r1 = subprocess.run(
                    ["pacman", "-Ss", self.query], capture_output=True, text=True, timeout=30
                )
                pkgs.extend(self._parse_pacman_search(r1.stdout))
                seen = {(p["name"], p["repository"]) for p in pkgs}
                helper = _aur_helper_cmd()
                if helper:
                    r2 = subprocess.run(
                        [helper, "-Ss", self.query], capture_output=True, text=True, timeout=45
                    )
                    for p in self._parse_aur_search(r2.stdout):
                        key = (p["name"], p["repository"])
                        if key not in seen:
                            pkgs.append(p)
                            seen.add(key)
            else:
                result = subprocess.run(
                    ["pacman", "-Qs", self.query], capture_output=True, text=True, timeout=30
                )
                pkgs = []
                lines = result.stdout.split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line and not line.startswith(" "):
                        parts = line.split(" ", 1)
                        if len(parts) >= 2:
                            loc = parts[0]
                            if "/" in loc:
                                name = loc.split("/", 1)[1]
                            else:
                                name = loc
                            version = parts[1].strip() if len(parts) >= 2 else ""
                            description = ""
                            if i + 1 < len(lines) and lines[i + 1].startswith("    "):
                                description = lines[i + 1].strip()
                                i += 1
                            pkgs.append(
                                {
                                    "name": name,
                                    "pkg": name,
                                    "version": version,
                                    "description": description,
                                    "repository": "installed",
                                    "installed": True,
                                }
                            )
                    i += 1
            GLib.idle_add(self.on_results, pkgs)
        except Exception:
            GLib.idle_add(self.on_results, [])


# ----------------------------
# GTK UI
# ----------------------------

class PKGERWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"{APP_NAME} {APP_VERSION}")
        self.set_default_size(1100, 720)
        self.set_resizable(True)
        try:
            self.set_icon_name(TRAY_ICON_NAME)
        except Exception:
            pass

        self._status_push_id = None
        self._tray_indicator = None
        self._tray_status_icon = None
        self._tray_menu = None
        self._tray_close_hides_window = False
        self._tray_ind_module = None

        self.current_worker = None
        self.search_worker = None
        self.sudo_password = None
        self.packages_cache = []
        self.repos_data = {}
        self.repos_worker = None
        self.updates_data = []
        self.updates_worker = None
        self.installed_versions = {}
        self.details_cache = {}
        self.icon_manager = PackageIconManager()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)
        self._apply_styling()

        header = self._create_header_bar()
        self.set_titlebar(header)

        self._package_paned = Gtk.HPaned()
        self._package_paned.set_position(340)
        root.pack_start(self._package_paned, True, True, 0)

        self._build_sidebar(self._package_paned)
        self._build_main_panel(self._package_paned)

        root.pack_start(self._create_progress(), False, False, 0)

        self.statusbar = Gtk.Statusbar()
        self.statusbar_context = self.statusbar.get_context_id("pkger")
        self._status_push_id = self.statusbar.push(self.statusbar_context, "Ready")
        root.pack_start(self.statusbar, False, False, 0)

        GLib.idle_add(self.load_installed_packages)
        GLib.idle_add(self.load_repos_data)
        GLib.idle_add(self.load_updates_data)
        GLib.idle_add(self._refresh_system_stats)
        GLib.idle_add(self._setup_tray_icon)

    def _build_tray_menu(self):
        menu = Gtk.Menu()
        show_item = Gtk.MenuItem(label="Show PKGER")
        show_item.set_tooltip_text("Restore and focus the main window.")
        show_item.connect("activate", lambda *_: self._tray_show_window())
        menu.append(show_item)
        refresh_item = Gtk.MenuItem(label="Refresh package list")
        refresh_item.set_tooltip_text("Same as File → Refresh package list.")
        refresh_item.connect("activate", lambda *_: self.refresh_packages())
        menu.append(refresh_item)
        upd_item = Gtk.MenuItem(label="Update system…")
        upd_item.set_tooltip_text("Run a full system update (pacman -Syu).")
        upd_item.connect("activate", lambda *_: self.update_system())
        menu.append(upd_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.set_tooltip_text("Exit PKGER completely.")
        quit_item.connect("activate", lambda *_: self._quit_application())
        menu.append(quit_item)
        menu.show_all()
        return menu

    def _setup_tray_icon(self):
        """Tray icon with context menu (Ayatana AppIndicator, legacy AppIndicator, or Gtk.StatusIcon)."""
        self._tray_menu = self._build_tray_menu()

        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppInd

            ind = AppInd.Indicator.new(
                "pkger",
                TRAY_ICON_NAME,
                AppInd.IndicatorCategory.APPLICATION_STATUS,
            )
            ind.set_status(AppInd.IndicatorStatus.ACTIVE)
            ind.set_menu(self._tray_menu)
            try:
                ind.set_title(APP_NAME)
            except Exception:
                pass
            self._tray_indicator = ind
            self._tray_ind_module = AppInd
            self._tray_close_hides_window = True
            self.connect("delete-event", self._on_delete_event_tray_hide)
            self.connect("destroy", lambda *_: self._cleanup_tray_icon())
            return
        except (ValueError, ImportError, AttributeError):
            pass

        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppInd

            ind = AppInd.Indicator.new(
                "pkger",
                TRAY_ICON_NAME,
                AppInd.IndicatorCategory.APPLICATION_STATUS,
            )
            ind.set_status(AppInd.IndicatorStatus.ACTIVE)
            ind.set_menu(self._tray_menu)
            self._tray_indicator = ind
            self._tray_ind_module = AppInd
            self._tray_close_hides_window = True
            self.connect("delete-event", self._on_delete_event_tray_hide)
            self.connect("destroy", lambda *_: self._cleanup_tray_icon())
            return
        except (ValueError, ImportError, AttributeError):
            pass

        if hasattr(Gtk, "StatusIcon"):
            try:
                si = Gtk.StatusIcon()
                si.set_from_icon_name(TRAY_ICON_NAME)
                si.set_tooltip_text(f"{APP_NAME} {APP_VERSION}")
                si.connect("popup-menu", self._on_status_icon_popup_menu)
                si.connect("activate", self._tray_show_window)
                self._tray_status_icon = si
                self._tray_close_hides_window = True
                self.connect("delete-event", self._on_delete_event_tray_hide)
                self.connect("destroy", lambda *_: self._cleanup_tray_icon())
            except Exception:
                pass

    def _cleanup_tray_icon(self):
        """Release tray resources (safe to call more than once)."""
        if self._tray_indicator is not None:
            if self._tray_ind_module is not None:
                try:
                    self._tray_indicator.set_status(self._tray_ind_module.IndicatorStatus.PASSIVE)
                except Exception:
                    pass
            self._tray_indicator = None
            self._tray_ind_module = None
        if self._tray_status_icon is not None:
            try:
                self._tray_status_icon.set_visible(False)
            except Exception:
                pass
            self._tray_status_icon = None

    def _quit_application(self):
        """Terminate PKGER completely (File → Exit, tray Quit)."""
        self._tray_close_hides_window = False
        self.destroy()

    def _on_status_icon_popup_menu(self, icon, button, activate_time):
        if self._tray_menu:
            self._tray_menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def _on_delete_event_tray_hide(self, _widget, _event):
        if not self._tray_close_hides_window:
            return False
        self.hide()
        return True

    def _tray_show_window(self, *_args):
        self.show_all()
        self.present()

    def _apply_styling(self):
        """Apply light application CSS (tree font, notebook tabs, dim labels)."""
        try:
            css_provider = Gtk.CssProvider()
            css = """
            treeview { font-size: 11px; }
            notebook tab { padding: 6px 10px; }
            .dim-label { opacity: 0.75; }
            """
            css_provider.load_from_data(css.encode())
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except Exception:
            pass

    def _create_header_bar(self):
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        header.set_subtitle(f"Version {APP_VERSION} · {APP_DEVELOPER}")

        menu_btn = Gtk.MenuButton()
        menu_btn.set_tooltip_text("Main menu: file actions, tools, and help.")
        menu_icon = Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON)
        menu_btn.add(menu_icon)
        menu = Gtk.Menu()

        file_item = Gtk.MenuItem(label="File")
        file_item.set_tooltip_text("Open, export, refresh, or exit.")
        file_menu = Gtk.Menu()
        refresh_item = Gtk.MenuItem(label="Refresh package list")
        refresh_item.set_tooltip_text(
            "Re-run the current sidebar search, or reload all installed packages if the search box is empty."
        )
        refresh_item.connect("activate", lambda *_: self.refresh_packages())
        file_menu.append(refresh_item)
        file_menu.append(Gtk.SeparatorMenuItem())
        browse_item = Gtk.MenuItem(label="Browse local package…")
        browse_item.set_tooltip_text(
            "Pick a .pkg.tar.zst or .pkg.tar.xz file from disk and install it with pacman -U (needs sudo)."
        )
        browse_item.connect("activate", lambda *_: self.browse_local_package())
        file_menu.append(browse_item)
        file_menu.append(Gtk.SeparatorMenuItem())
        export_list_item = Gtk.MenuItem(label="Export package list…")
        export_list_item.set_tooltip_text(
            "Save the current sidebar package list (as shown) to a text file."
        )
        export_list_item.connect("activate", lambda *_: self._export_package_list())
        file_menu.append(export_list_item)
        file_menu.append(Gtk.SeparatorMenuItem())
        exit_item = Gtk.MenuItem(label="Exit")
        exit_item.set_tooltip_text("Quit PKGER completely.")
        exit_item.connect("activate", lambda *_: self._quit_application())
        file_menu.append(exit_item)
        file_item.set_submenu(file_menu)

        tools_item = Gtk.MenuItem(label="Tools")
        tools_item.set_tooltip_text("System updates, cache, dependency refresh, local file search.")
        tools_menu = Gtk.Menu()
        update_item = Gtk.MenuItem(label="Update system")
        update_item.set_tooltip_text("Run pacman -Syu --noconfirm to upgrade all official packages (needs sudo).")
        update_item.connect("activate", lambda *_: self.update_system())
        tools_menu.append(update_item)
        clean_item = Gtk.MenuItem(label="Clean package cache")
        clean_item.set_tooltip_text(
            "Run pacman -Sc --noconfirm to remove uninstalled packages from /var/cache/pacman/pkg (needs sudo)."
        )
        clean_item.connect("activate", lambda *_: self.clean_cache())
        tools_menu.append(clean_item)
        fix_item = Gtk.MenuItem(label="Fix broken dependencies")
        fix_item.set_tooltip_text(
            "Refresh databases with pacman -Syy (may help if metadata is stale; needs sudo)."
        )
        fix_item.connect("activate", lambda *_: self.fix_broken_dependencies())
        tools_menu.append(fix_item)
        tools_menu.append(Gtk.SeparatorMenuItem())
        local_pkg_item = Gtk.MenuItem(label="Search local .pkg files…")
        local_pkg_item.set_tooltip_text(
            "Scan common folders for matching package filenames and show them in the sidebar list."
        )
        local_pkg_item.connect("activate", lambda *_: self._search_local_packages_dialog())
        tools_menu.append(local_pkg_item)
        tools_menu.append(Gtk.SeparatorMenuItem())
        sys_tab_item = Gtk.MenuItem(label="System and maintenance tab")
        sys_tab_item.set_tooltip_text("Switch to the System tab (logs, news, maintenance buttons).")
        sys_tab_item.connect("activate", lambda *_: self.notebook.set_current_page(4))
        tools_menu.append(sys_tab_item)
        tools_item.set_submenu(tools_menu)

        help_item = Gtk.MenuItem(label="Help")
        help_item.set_tooltip_text("About, diagnostics, guide, and shortcuts.")
        help_menu = Gtk.Menu()
        about_item = Gtk.MenuItem(label="About")
        about_item.set_tooltip_text("Application name, version, and credits.")
        about_item.connect("activate", lambda *_: self.show_about())
        help_menu.append(about_item)
        help_menu.append(Gtk.SeparatorMenuItem())
        sys_info_item = Gtk.MenuItem(label="System information")
        sys_info_item.set_tooltip_text("Hostname, user, architecture, and installed package count.")
        sys_info_item.connect("activate", lambda *_: self._show_system_info())
        help_menu.append(sys_info_item)
        stats_item = Gtk.MenuItem(label="Package statistics")
        stats_item.set_tooltip_text("Counts for the current sidebar list grouped by repository field.")
        stats_item.connect("activate", lambda *_: self._show_package_stats())
        help_menu.append(stats_item)
        guide_item = Gtk.MenuItem(label="User guide")
        guide_item.set_tooltip_text("Short overview of main PKGER workflows.")
        guide_item.connect("activate", lambda *_: self._show_user_guide())
        help_menu.append(guide_item)
        keys_item = Gtk.MenuItem(label="Keyboard shortcuts")
        keys_item.set_tooltip_text("Common keys and focus behavior.")
        keys_item.connect("activate", lambda *_: self._show_keyboard_shortcuts())
        help_menu.append(keys_item)
        help_item.set_submenu(help_menu)

        for w in (file_item, tools_item, help_item):
            menu.append(w)
        menu.show_all()
        menu_btn.set_popup(menu)
        header.pack_end(menu_btn)
        return header

    def _build_sidebar(self, paned):
        sidebar_frame = Gtk.Frame()
        sidebar_frame.set_shadow_type(Gtk.ShadowType.IN)
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar_box.set_margin_top(10)
        sidebar_box.set_margin_bottom(10)
        sidebar_box.set_margin_start(10)
        sidebar_box.set_margin_end(10)
        sidebar_frame.add(sidebar_box)
        sidebar_frame.set_size_request(200, -1)

        list_title = Gtk.Label()
        list_title.set_markup("<b>Package list</b>")
        list_title.set_xalign(0.0)
        sidebar_box.pack_start(list_title, False, False, 0)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search packages…")
        self.search_entry.set_tooltip_text(
            "Type a substring or keyword, then press Enter or click Search. "
            "Uses the scope chosen in the drop-down (official / AUR / installed / all)."
        )
        self.search_entry.connect("activate", lambda *_: self.search_packages())
        sidebar_box.pack_start(self.search_entry, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_type = Gtk.ComboBoxText()
        self.search_type.append_text("Official")
        self.search_type.append_text("AUR")
        self.search_type.append_text("Installed")
        self.search_type.append_text("All (official + AUR)")
        self.search_type.set_active(0)
        self.search_type.set_tooltip_text(
            "Official: pacman -Ss (sync databases). "
            "AUR: yay or paru -Ss. Installed: pacman -Qs. "
            "All: merge official and AUR results (deduplicated)."
        )
        self.search_btn = Gtk.Button.new_with_label("Search")
        self.search_btn.set_tooltip_text("Run the search for the text in the box using the selected scope.")
        self.search_btn.connect("clicked", lambda *_: self.search_packages())
        row.pack_start(self.search_type, True, True, 0)
        row.pack_start(self.search_btn, False, False, 0)
        sidebar_box.pack_start(row, False, False, 0)

        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str, bool)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(True)
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.get_selection().connect("changed", self.on_selection_changed)

        rp = Gtk.CellRendererPixbuf()
        rp.set_property("xpad", 2)
        rp.set_property("ypad", 1)
        col0 = Gtk.TreeViewColumn("", rp, pixbuf=0)
        col0.set_min_width(20)
        col0.set_max_width(26)
        col0.set_sort_column_id(1)
        r1 = Gtk.CellRendererText()
        r2 = Gtk.CellRendererText()
        r3 = Gtk.CellRendererText()
        r4 = Gtk.CellRendererText()
        r5 = Gtk.CellRendererText()
        r5.set_property("ellipsize", Pango.EllipsizeMode.END)
        col1 = Gtk.TreeViewColumn("Name", r1, text=1)
        col2 = Gtk.TreeViewColumn("Version", r2, text=2)
        col3 = Gtk.TreeViewColumn("Description", r5, text=3)
        col4 = Gtk.TreeViewColumn("Repo", r3, text=4)
        col5 = Gtk.TreeViewColumn("Installed", r4)
        col5.set_cell_data_func(
            r4,
            lambda col, cell, model, itr, data=None: cell.set_property(
                "text", "Yes" if model[itr][5] else "No"
            ),
        )
        col1.set_sort_column_id(1)
        col2.set_sort_column_id(2)
        col3.set_sort_column_id(3)
        col4.set_sort_column_id(4)
        col5.set_sort_column_id(5)
        col1.set_resizable(True)
        col1.set_min_width(80)
        col1.set_expand(True)
        col2.set_resizable(True)
        col2.set_min_width(72)
        col3.set_resizable(True)
        col3.set_min_width(100)
        col3.set_expand(True)
        col4.set_resizable(True)
        col4.set_min_width(56)
        col5.set_resizable(True)
        col5.set_min_width(72)
        for c in (col0, col1, col2, col3, col4, col5):
            self.tree.append_column(c)
        self.tree.set_search_column(1)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        sidebar_box.pack_start(sw, True, True, 0)

        self.count_label = Gtk.Label(label="0 packages")
        self.count_label.set_xalign(0.0)
        self.count_label.set_tooltip_text("Number of rows currently shown in the package list.")
        self.count_label.get_style_context().add_class("dim-label")
        sidebar_box.pack_start(self.count_label, False, False, 0)

        sidebar_box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        qa = Gtk.Label()
        qa.set_markup("<b>Quick actions</b>")
        qa.set_xalign(0.0)
        sidebar_box.pack_start(qa, False, False, 0)

        self.update_btn = Gtk.Button.new_with_label("Update system")
        self.update_btn.set_tooltip_text(
            "Full official upgrade: pacman -Syu --noconfirm (asks sudo; confirm in dialog)."
        )
        self.update_btn.get_style_context().add_class("suggested-action")
        self.update_btn.connect("clicked", lambda *_: self.update_system())

        self.clean_btn = Gtk.Button.new_with_label("Clean cache")
        self.clean_btn.set_tooltip_text(
            "Remove cached package files that are no longer installed (pacman -Sc --noconfirm)."
        )
        self.clean_btn.connect("clicked", lambda *_: self.clean_cache())

        self.browse_btn = Gtk.Button.new_with_label("Browse local package")
        self.browse_btn.set_tooltip_text(
            "Choose a .pkg.tar.zst or .pkg.tar.xz on disk and install with pacman -U."
        )
        self.browse_btn.connect("clicked", lambda *_: self.browse_local_package())

        self.refresh_all_btn = Gtk.Button.new_with_label("Refresh all")
        self.refresh_all_btn.set_tooltip_text(
            "Reload installed/search context, repository catalog (pacman -Sl), pending updates, and system stats."
        )
        self.refresh_all_btn.connect("clicked", lambda *_: self.refresh_all())

        self.reset_sort_btn = Gtk.Button.new_with_label("Reset list order")
        self.reset_sort_btn.set_tooltip_text(
            "Rebuild the sidebar list from the in-memory package cache (restores source order)."
        )
        self.reset_sort_btn.connect("clicked", lambda *_: self.reset_sorting())

        for b in (self.update_btn, self.clean_btn, self.browse_btn, self.refresh_all_btn, self.reset_sort_btn):
            sidebar_box.pack_start(b, False, False, 0)

        # resize=False: extra window width goes to the main panel; shrink=True: user may narrow the list
        paned.pack1(sidebar_frame, False, True)

    def _build_main_panel(self, paned):
        main_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        main_panel.set_margin_top(12)
        main_panel.set_margin_bottom(12)
        main_panel.set_margin_start(12)
        main_panel.set_margin_end(12)
        paned.pack2(main_panel, True, True)

        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)

        def _tab_lbl(text, tip):
            l = Gtk.Label(label=text)
            l.set_tooltip_text(tip)
            return l

        self.notebook.append_page(
            self._create_operations_page(),
            _tab_lbl("Operations", "Install / remove / reinstall and live command output for the selected package."),
        )
        self.notebook.append_page(
            self._create_details_page(),
            _tab_lbl("Details", "Metadata, description, dependencies, and export for the selected package."),
        )
        self.notebook.append_page(
            self._create_repos_page(),
            _tab_lbl(
                "Repositories",
                "Browse pacman -Sl by repo: filter, multi-select, install/remove, and per-row details.",
            ),
        )
        self.notebook.append_page(
            self._create_updates_page(),
            _tab_lbl("Updates", "Pending pacman -Qu upgrades: tick rows, apply, or remove orphan packages."),
        )
        self.notebook.append_page(
            self._create_system_tools_page(),
            _tab_lbl("System", "Disk/cache stats, maintenance commands, file DB search, pacman log, Arch news RSS."),
        )

        main_panel.pack_start(self.notebook, True, True, 0)

    def _create_operations_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        action_frame = Gtk.Frame()
        action_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        action_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        action_inner.set_margin_top(12)
        action_inner.set_margin_bottom(12)
        action_inner.set_margin_start(12)
        action_inner.set_margin_end(12)
        action_frame.add(action_inner)

        ah = Gtk.Label()
        ah.set_markup("<b>Selected package</b>")
        ah.set_xalign(0.0)
        action_inner.pack_start(ah, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.install_btn = Gtk.Button.new_with_label("Install")
        self.install_btn.set_tooltip_text(
            "Install the selected row: pacman -S for repos, yay/paru -S for AUR, pacman -U for local file rows."
        )
        self.install_btn.set_sensitive(False)
        self.install_btn.connect("clicked", lambda *_: self.install_package())
        self.remove_btn = Gtk.Button.new_with_label("Remove")
        self.remove_btn.set_tooltip_text(
            "Remove the selected package by name (pacman -R --noconfirm). Only enabled when marked installed."
        )
        self.remove_btn.set_sensitive(False)
        self.remove_btn.connect("clicked", lambda *_: self.remove_package())
        self.reinstall_btn = Gtk.Button.new_with_label("Reinstall")
        self.reinstall_btn.set_tooltip_text(
            "Reinstall sync packages with pacman -S, or AUR helpers for AUR rows (same as a fresh install)."
        )
        self.reinstall_btn.set_sensitive(False)
        self.reinstall_btn.connect("clicked", lambda *_: self.reinstall_package())
        for b in (self.install_btn, self.remove_btn, self.reinstall_btn):
            controls.pack_start(b, False, False, 0)
        action_inner.pack_start(controls, False, False, 0)
        outer.pack_start(action_frame, False, False, 0)

        logs_frame = Gtk.Frame()
        logs_frame.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
        logs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        logs_box.set_margin_top(10)
        logs_box.set_margin_bottom(10)
        logs_box.set_margin_start(10)
        logs_box.set_margin_end(10)
        logs_frame.add(logs_box)

        lt = Gtk.Label()
        lt.set_markup("<b>Transaction output</b>")
        lt.set_xalign(0.0)
        logs_box.pack_start(lt, False, False, 0)

        self.textbuf = Gtk.TextBuffer()
        self.textview = Gtk.TextView(buffer=self.textbuf)
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_tooltip_text(
            "Timestamped stream of the current or last package operation (stdout and stderr merged)."
        )
        out_sw = Gtk.ScrolledWindow()
        out_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        out_sw.set_min_content_height(200)
        out_sw.add(self.textview)
        logs_box.pack_start(out_sw, True, True, 0)
        outer.pack_start(logs_frame, True, True, 0)
        return outer

    def _create_updates_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.upd_select_all = Gtk.CheckButton(label="Select All")
        self.upd_select_all.set_tooltip_text("Check or uncheck every row in the updates list.")
        self.upd_select_all.connect("toggled", self._updates_toggle_all)
        self.upd_refresh_btn = Gtk.Button.new_with_label("Refresh Updates")
        self.upd_refresh_btn.set_tooltip_text("Re-run pacman -Qu to rebuild the list of pending official upgrades.")
        self.upd_refresh_btn.connect("clicked", lambda *_: self.load_updates_data())
        self.upd_apply_btn = Gtk.Button.new_with_label("Apply Selected")
        self.upd_apply_btn.set_tooltip_text(
            "Install selected upgrades in one transaction: pacman -S --noconfirm <names> (needs sudo)."
        )
        self.upd_apply_btn.connect("clicked", self._updates_apply_selected)
        self.orphans_btn = Gtk.Button.new_with_label("Remove Orphans")
        self.orphans_btn.set_tooltip_text(
            "Remove dependency-only packages no longer required: pacman -Qtdq piped to pacman -Rns (needs sudo)."
        )
        self.orphans_btn.connect("clicked", self._remove_orphans)
        for w in (self.upd_select_all, self.upd_refresh_btn, self.upd_apply_btn, self.orphans_btn):
            toolbar.pack_start(w, False, False, 0)
        vbox.pack_start(toolbar, False, False, 0)

        self.upd_store = Gtk.ListStore(bool, GdkPixbuf.Pixbuf, str, str)  # selected, icon, name, old->new
        self.upd_view = Gtk.TreeView(model=self.upd_store)
        tgl = Gtk.CellRendererToggle(); tgl.set_activatable(True); tgl.connect("toggled", self._updates_toggle_row)
        colsel = Gtk.TreeViewColumn("Select", tgl, active=0)
        colsel.set_sort_column_id(2)
        upix = Gtk.CellRendererPixbuf()
        upix.set_property("xpad", 2)
        upix.set_property("ypad", 1)
        col_upd_icon = Gtk.TreeViewColumn("", upix, pixbuf=1)
        col_upd_icon.set_min_width(20)
        col_upd_icon.set_max_width(26)
        col_upd_icon.set_sort_column_id(2)
        rname = Gtk.CellRendererText()
        colname = Gtk.TreeViewColumn("Package", rname, text=2)
        colname.set_sort_column_id(2)
        colname.set_resizable(True)
        colname.set_min_width(120)
        colname.set_expand(True)
        rver = Gtk.CellRendererText()
        colver = Gtk.TreeViewColumn("Update", rver, text=3)
        colver.set_sort_column_id(3)
        colver.set_resizable(True)
        colver.set_min_width(140)
        for c in (colsel, col_upd_icon, colname, colver):
            self.upd_view.append_column(c)
        sw = Gtk.ScrolledWindow(); sw.add(self.upd_view)
        vbox.pack_start(sw, True, True, 0)
        return vbox

    def _create_details_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        grid.set_column_homogeneous(False)

        def add_row(r, label_text):
            lbl = Gtk.Label(label=label_text)
            lbl.set_xalign(0.0)
            val = Gtk.Label(label="-")
            val.set_xalign(0.0)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(val, 1, r, 1, 1)
            return val

        self.d_name = add_row(0, "Name:")
        self.d_version = add_row(1, "Version:")
        self.d_repo = add_row(2, "Repository:")
        self.d_installed = add_row(3, "Installed:")
        self.d_size = add_row(4, "Installed Size:")
        self.d_license = add_row(5, "License:")
        self.d_url = add_row(6, "URL:")

        outer.pack_start(grid, False, False, 0)

        desc_label = Gtk.Label(label="Description")
        desc_label.set_xalign(0.0)
        outer.pack_start(desc_label, False, False, 0)

        self.desc_buf = Gtk.TextBuffer()
        self.desc_view = Gtk.TextView(buffer=self.desc_buf)
        self.desc_view.set_editable(False)
        self.desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw1 = Gtk.ScrolledWindow()
        sw1.set_min_content_height(90)
        sw1.add(self.desc_view)
        outer.pack_start(sw1, False, False, 0)

        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        deps_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        deps_lbl = Gtk.Label(label="Dependencies")
        deps_lbl.set_xalign(0.0)
        deps_v.pack_start(deps_lbl, False, False, 0)
        self.deps_buf = Gtk.TextBuffer()
        deps_view = Gtk.TextView(buffer=self.deps_buf)
        deps_view.set_editable(False)
        deps_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        sw2 = Gtk.ScrolledWindow(); sw2.set_min_content_height(70); sw2.add(deps_view)
        deps_v.pack_start(sw2, True, True, 0)

        rdeps_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rdeps_lbl = Gtk.Label(label="Required By")
        rdeps_lbl.set_xalign(0.0)
        rdeps_v.pack_start(rdeps_lbl, False, False, 0)
        self.rdeps_buf = Gtk.TextBuffer()
        rdeps_view = Gtk.TextView(buffer=self.rdeps_buf)
        rdeps_view.set_editable(False)
        rdeps_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        sw3 = Gtk.ScrolledWindow(); sw3.set_min_content_height(70); sw3.add(rdeps_view)
        rdeps_v.pack_start(sw3, True, True, 0)

        hb.pack_start(deps_v, True, True, 0)
        hb.pack_start(rdeps_v, True, True, 0)
        outer.pack_start(hb, True, True, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        copy_btn = Gtk.Button.new_with_label("Copy Details")
        copy_btn.set_tooltip_text("Copy the visible details block to the clipboard as plain text.")
        export_btn = Gtk.Button.new_with_label("Export Details")
        export_btn.set_tooltip_text("Save the same text block to a file you choose.")
        open_btn = Gtk.Button.new_with_label("Open Homepage")
        open_btn.set_tooltip_text("Open the URL field in your default browser (xdg-open).")
        load_deps_btn = Gtk.Button.new_with_label("Load Dependencies")
        load_deps_btn.set_tooltip_text(
            "Run pactree and pactree -r for the selected package (slower; refreshes dependency panes)."
        )
        copy_btn.connect("clicked", self._copy_details)
        export_btn.connect("clicked", self._export_details)
        open_btn.connect("clicked", self._open_homepage)
        load_deps_btn.connect("clicked", self._load_current_deps)
        for b in (copy_btn, export_btn, open_btn, load_deps_btn):
            actions.pack_start(b, False, False, 0)
        outer.pack_start(actions, False, False, 0)

        det_ops = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.details_install_btn = Gtk.Button.new_with_label("Install (from details)")
        self.details_install_btn.set_tooltip_text(
            "Install using the Name and Repository fields shown above (same rules as the Install button)."
        )
        self.details_install_btn.connect("clicked", self._install_from_details_tab)
        self.details_remove_btn = Gtk.Button.new_with_label("Remove (from details)")
        self.details_remove_btn.set_tooltip_text(
            "Remove using the package name from the Name field (pacman -R)."
        )
        self.details_remove_btn.connect("clicked", self._remove_from_details_tab)
        for b in (self.details_install_btn, self.details_remove_btn):
            det_ops.pack_start(b, False, False, 0)
        self.details_install_btn.set_sensitive(False)
        self.details_remove_btn.set_sensitive(False)
        outer.pack_start(det_ops, False, False, 0)

        return outer

    def _create_repos_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Toolbar: search/filter + actions
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.repo_search = Gtk.Entry()
        self.repo_search.set_placeholder_text("Filter packages...")
        self.repo_search.set_tooltip_text(
            "Filter the right-hand package table for the active repository (substring match on package name)."
        )
        self.repo_search.connect("activate", lambda *_: self._filter_repos_tree())
        self.repo_filter_installed = Gtk.CheckButton(label="Installed only")
        self.repo_filter_installed.set_tooltip_text("Only list packages already installed from this repository.")
        self.repo_filter_installed.connect("toggled", lambda *_: self._filter_repos_tree())
        self.repo_show_all = Gtk.CheckButton(label="Show All")
        self.repo_show_all.set_tooltip_text(
            "When off, show at most 200 matching rows for speed; when on, list every match (may be slow)."
        )
        self.repo_show_all.connect("toggled", lambda *_: self._filter_repos_tree())
        self.repo_install_btn = Gtk.Button.new_with_label("Install Selected")
        self.repo_install_btn.set_tooltip_text(
            "pacman -S --noconfirm for all ticked rows as repo/pkgname for the selected database."
        )
        self.repo_install_btn.connect("clicked", self._repos_install_selected)
        self.repo_remove_btn = Gtk.Button.new_with_label("Remove Selected")
        self.repo_remove_btn.set_tooltip_text(
            "pacman -R --noconfirm on the bare package names for all ticked rows (needs sudo)."
        )
        self.repo_remove_btn.connect("clicked", self._repos_remove_selected)
        self.repo_export_btn = Gtk.Button.new_with_label("Export List")
        self.repo_export_btn.set_tooltip_text(
            "Write the full in-memory repo catalog (all repos from pacman -Sl) to a text file."
        )
        self.repo_export_btn.connect("clicked", self._repos_export_list)
        for w in (self.repo_search, self.repo_filter_installed, self.repo_show_all, self.repo_install_btn, self.repo_remove_btn, self.repo_export_btn):
            toolbar.pack_start(w, False, False, 0)
        vbox.pack_start(toolbar, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Left: vertical repos list
        self.repos_list = Gtk.ListStore(GdkPixbuf.Pixbuf, str)  # icon, repo name
        self.repos_view = Gtk.TreeView(model=self.repos_list)
        self.repos_view.set_tooltip_text("Sync database names from pacman -Sl; selecting one fills the package table.")
        self.repos_view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        rr_pix = Gtk.CellRendererPixbuf()
        rr_pix.set_property("xpad", 2)
        rr_pix.set_property("ypad", 1)
        col_r_icon = Gtk.TreeViewColumn("", rr_pix, pixbuf=0)
        col_r_icon.set_min_width(20)
        col_r_icon.set_max_width(26)
        col_r_icon.set_sort_column_id(1)
        rrepo = Gtk.CellRendererText()
        colrepo = Gtk.TreeViewColumn("Repositories", rrepo, text=1)
        colrepo.set_sort_column_id(1)
        colrepo.set_resizable(True)
        colrepo.set_min_width(96)
        colrepo.set_expand(True)
        for c in (col_r_icon, colrepo):
            self.repos_view.append_column(c)
        self.repos_view.get_selection().connect("changed", self._on_repo_selected)
        swl = Gtk.ScrolledWindow(); swl.set_min_content_width(140); swl.add(self.repos_view)
        hbox.pack_start(swl, False, False, 0)

        # Right: vertical paned with table top and details bottom
        right_paned = Gtk.VPaned()

        # Packages table with checkboxes
        self.repo_pkgs_store = Gtk.ListStore(bool, GdkPixbuf.Pixbuf, str, bool)  # selected, icon, name, installed
        self.repo_pkgs_view = Gtk.TreeView(model=self.repo_pkgs_store)
        self.repo_pkgs_view.set_tooltip_text(
            "Packages in the selected repository: tick boxes for batch install/remove; click a row for details below."
        )
        self.repo_pkgs_view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.repo_pkgs_view.get_selection().connect("changed", self._on_repo_pkg_selection)

        rtoggle = Gtk.CellRendererToggle(); rtoggle.set_activatable(True); rtoggle.connect("toggled", self._on_repo_pkg_toggled)
        colsel = Gtk.TreeViewColumn("Select", rtoggle, active=0)
        colsel.set_sort_column_id(2)
        rp_repo = Gtk.CellRendererPixbuf()
        rp_repo.set_property("xpad", 2)
        rp_repo.set_property("ypad", 1)
        col_rp_icon = Gtk.TreeViewColumn("", rp_repo, pixbuf=1)
        col_rp_icon.set_min_width(20)
        col_rp_icon.set_max_width(26)
        col_rp_icon.set_sort_column_id(2)
        rname = Gtk.CellRendererText(); colname = Gtk.TreeViewColumn("Package", rname, text=2)
        colname.set_sort_column_id(2)
        colname.set_resizable(True)
        colname.set_min_width(100)
        colname.set_expand(True)
        rinst = Gtk.CellRendererText(); colinst = Gtk.TreeViewColumn("Installed", rinst)
        colinst.set_cell_data_func(rinst, lambda c, cell, m, itr, d=None: cell.set_property('text', 'Yes' if m[itr][3] else 'No'))
        colinst.set_sort_column_id(3)
        colinst.set_resizable(True)
        colinst.set_min_width(72)
        for c in (colsel, col_rp_icon, colname, colinst):
            self.repo_pkgs_view.append_column(c)

        sw_top = Gtk.ScrolledWindow(); sw_top.add(self.repo_pkgs_view)
        right_paned.add1(sw_top)

        # Details area
        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        det_title = Gtk.Label(label="Package Details (Repositories)")
        det_title.set_xalign(0.0)
        details_box.pack_start(det_title, False, False, 0)

        self.repo_d_name = Gtk.Label(label="Name: -"); self.repo_d_name.set_xalign(0.0)
        self.repo_d_version = Gtk.Label(label="Version: -"); self.repo_d_version.set_xalign(0.0)
        self.repo_d_repo = Gtk.Label(label="Repository: -"); self.repo_d_repo.set_xalign(0.0)
        self.repo_d_installed = Gtk.Label(label="Installed: -"); self.repo_d_installed.set_xalign(0.0)
        self.repo_d_url = Gtk.Label(label="URL: -"); self.repo_d_url.set_xalign(0.0)
        for w in (self.repo_d_name, self.repo_d_version, self.repo_d_repo, self.repo_d_installed, self.repo_d_url):
            details_box.pack_start(w, False, False, 0)

        self.repo_desc_buf = Gtk.TextBuffer(); self.repo_desc_view = Gtk.TextView(buffer=self.repo_desc_buf)
        self.repo_desc_view.set_editable(False); self.repo_desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw_desc = Gtk.ScrolledWindow(); sw_desc.set_min_content_height(90); sw_desc.add(self.repo_desc_view)
        details_box.pack_start(sw_desc, True, True, 0)

        right_paned.add2(details_box)
        right_paned.set_position(220)

        hbox.pack_start(right_paned, True, True, 0)

        vbox.pack_start(hbox, True, True, 0)
        return vbox

    def _create_system_tools_page(self):
        self._system_tool_buttons = []
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(4)
        outer.set_margin_bottom(8)
        scroll.add(outer)

        def frame_block(title_plain):
            f = Gtk.Frame()
            f.set_shadow_type(Gtk.ShadowType.ETCHED_IN)
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            b.set_margin_top(10)
            b.set_margin_bottom(10)
            b.set_margin_start(10)
            b.set_margin_end(10)
            tl = Gtk.Label()
            esc = GLib.markup_escape_text(title_plain, -1)
            tl.set_markup(f"<b>{esc}</b>")
            tl.set_xalign(0.0)
            b.pack_start(tl, False, False, 0)
            f.add(b)
            return f, b

        # Status
        f1, b1 = frame_block("System status")
        self.system_stats_label = Gtk.Label(label="Loading…")
        self.system_stats_label.set_xalign(0.0)
        self.system_stats_label.set_line_wrap(True)
        self.system_stats_label.set_tooltip_text(
            "Live summary: pacman cache size, root filesystem usage, pacman version line, pending -Qu count, helper availability."
        )
        b1.pack_start(self.system_stats_label, False, False, 0)
        btn_stats = Gtk.Button.new_with_label("Refresh status")
        btn_stats.set_tooltip_text("Recompute the system status lines above (runs du, df, pacman in the background).")
        btn_stats.connect("clicked", self._refresh_system_stats)
        self._system_tool_buttons.append(btn_stats)
        b1.pack_start(btn_stats, False, False, 0)
        outer.pack_start(f1, False, False, 0)

        # Maintenance
        f2, b2 = frame_block("Maintenance (requires authentication when needed)")
        g = Gtk.Grid(column_spacing=8, row_spacing=8)
        row = 0

        def add_btn(label, handler, col, r, tip, cs=1):
            btn = Gtk.Button.new_with_label(label)
            btn.set_tooltip_text(tip)
            btn.connect("clicked", handler)
            self._system_tool_buttons.append(btn)
            g.attach(btn, col, r, cs, 1)

        add_btn(
            "Sync databases (pacman -Sy)",
            self._tool_sync_databases,
            0,
            row,
            "Refresh sync databases only (no package upgrade). Runs pacman -Sy --noconfirm with sudo.",
        )
        add_btn(
            "Full system update (pacman)",
            self._tool_update_official,
            1,
            row,
            "Same as sidebar Update system: pacman -Syu --noconfirm with sudo.",
        )
        row += 1
        add_btn(
            "Full update + AUR (yay/paru)",
            self._tool_update_full_aur,
            0,
            row,
            "Runs yay -Syu or paru -Syu with --noconfirm (not via sudo pipe; helper handles privileges).",
        )
        add_btn(
            "Deep clean cache (pacman -Scc)",
            self._tool_deep_clean_cache,
            1,
            row,
            "Remove all files from the package cache directory (pacman -Scc --noconfirm).",
        )
        row += 1
        add_btn(
            "Refresh keyring (pacman-key)",
            self._tool_refresh_keyring,
            0,
            row,
            "Refresh PGP keys with pacman-key --refresh-keys (needs network and sudo).",
        )
        add_btn(
            "Verify installed files (pacman -Qk)",
            self._tool_verify_files,
            1,
            row,
            "Check that installed files match the package database (can be slow; output in Operations log).",
        )
        row += 1
        add_btn(
            "Check database integrity (pacman -Dk)",
            self._tool_check_database,
            0,
            row,
            "Test local database consistency; output is streamed to the Operations log.",
        )
        add_btn(
            "Rank mirrors (reflector)",
            self._tool_reflector,
            1,
            row,
            "Overwrite /etc/pacman.d/mirrorlist using reflector (requires reflector package and sudo).",
        )
        row += 1
        add_btn(
            "Export explicit package list",
            self._tool_export_explicit,
            0,
            row,
            "Save pacman -Qqe output to a file (explicitly installed packages only).",
        )
        add_btn(
            "Reload pacman log",
            self._tool_reload_pacman_log,
            1,
            row,
            "Show the last lines of /var/log/pacman.log in the log viewer below.",
        )
        b2.pack_start(g, False, False, 0)
        outer.pack_start(f2, False, False, 0)

        # File search
        f3, b3 = frame_block("Files & packages")
        h3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.file_search_entry = Gtk.Entry()
        self.file_search_entry.set_placeholder_text("Search file in repos (pacman -Fs) — sync first: pacman -Fy")
        self.file_search_entry.set_tooltip_text(
            "Query the file database (pacman -Fs). Run sudo pacman -Fy first if you get no results."
        )
        self.file_search_entry.connect("activate", self._tool_search_file_repos)
        btn_fs = Gtk.Button.new_with_label("Search")
        btn_fs.set_tooltip_text("Run pacman -Fs with the text from the field on the left.")
        btn_fs.connect("clicked", self._tool_search_file_repos)
        self._system_tool_buttons.append(btn_fs)
        h3.pack_start(self.file_search_entry, True, True, 0)
        h3.pack_start(btn_fs, False, False, 0)
        b3.pack_start(h3, False, False, 0)
        h4 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.file_owns_entry = Gtk.Entry()
        self.file_owns_entry.set_placeholder_text("Full path, e.g. /usr/bin/bash")
        self.file_owns_entry.set_tooltip_text("Absolute path to any file; runs pacman -Qo to show owning package.")
        self.file_owns_entry.connect("activate", self._tool_query_owns)
        btn_own = Gtk.Button.new_with_label("Who owns")
        btn_own.set_tooltip_text("Run pacman -Qo on the path in the field on the left.")
        btn_own.connect("clicked", self._tool_query_owns)
        self._system_tool_buttons.append(btn_own)
        h4.pack_start(self.file_owns_entry, True, True, 0)
        h4.pack_start(btn_own, False, False, 0)
        b3.pack_start(h4, False, False, 0)
        self.file_tools_buf = Gtk.TextBuffer()
        fv = Gtk.TextView(buffer=self.file_tools_buf)
        fv.set_editable(False)
        fv.set_monospace(True)
        fv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        fv.set_tooltip_text("Output of the last file-database search or ownership query from this section.")
        swf = Gtk.ScrolledWindow()
        swf.set_min_content_height(100)
        swf.add(fv)
        b3.pack_start(swf, False, False, 0)
        outer.pack_start(f3, False, False, 0)

        # Pacman log
        f4, b4 = frame_block("Pacman log (tail)")
        self.pacman_log_buf = Gtk.TextBuffer()
        plv = Gtk.TextView(buffer=self.pacman_log_buf)
        plv.set_editable(False)
        plv.set_monospace(True)
        plv.set_wrap_mode(Gtk.WrapMode.NONE)
        swp = Gtk.ScrolledWindow()
        swp.set_min_content_height(160)
        swp.add(plv)
        b4.pack_start(swp, False, False, 0)
        outer.pack_start(f4, False, False, 0)

        # Arch news
        f5, b5 = frame_block("Arch Linux news")
        btn_news = Gtk.Button.new_with_label("Load latest news (RSS)")
        btn_news.set_tooltip_text("Fetch titles and links from the official Arch Linux news RSS feed (needs network).")
        btn_news.connect("clicked", self._tool_load_arch_news)
        self._system_tool_buttons.append(btn_news)
        b5.pack_start(btn_news, False, False, 0)
        self.arch_news_buf = Gtk.TextBuffer()
        nv = Gtk.TextView(buffer=self.arch_news_buf)
        nv.set_editable(False)
        nv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        swn = Gtk.ScrolledWindow()
        swn.set_min_content_height(180)
        swn.add(nv)
        b5.pack_start(swn, False, False, 0)
        outer.pack_start(f5, False, False, 0)

        GLib.idle_add(self._tool_reload_pacman_log)
        return scroll

    def _create_progress(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self.progress_label = Gtk.Label(label="Ready")
        self.progress_label.set_xalign(0.0)
        self.progress_label.set_tooltip_text("Short status for the last or current long-running action.")
        v.pack_start(self.progress_label, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        self.progress.set_no_show_all(True)
        self.progress.set_tooltip_text(
            "Rough progress for streaming pacman/AUR operations (heuristic from output keywords)."
        )
        v.pack_start(self.progress, False, False, 0)

        return v

    # Output helpers
    def append_output(self, text):
        iter_end = self.textbuf.get_end_iter()
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.textbuf.insert(iter_end, f"[{timestamp}] {text}\n")
        mark = self.textbuf.create_mark(None, self.textbuf.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def set_status(self, message):
        self.progress_label.set_text(message)
        if self._status_push_id is not None:
            self.statusbar.remove(self.statusbar_context, self._status_push_id)
        self._status_push_id = self.statusbar.push(self.statusbar_context, message)

    def set_progress_value(self, value):
        if value <= 0:
            self.progress.hide()
        else:
            self.progress.show()
            self.progress.set_fraction(max(0.0, min(1.0, value / 100.0)))
            self.progress.set_text(f"{int(value)}%")

    # Data & UI updates
    def _pkg_row_icon_key(self, pkg):
        if pkg.get("pkg"):
            return pkg["pkg"]
        n = pkg.get("name", "")
        return n.split("/")[-1] if "/" in n else n

    def update_package_list(self, packages):
        self.packages_cache = packages
        self.store.clear()
        for pkg in packages:
            name = pkg.get("name", "")
            version = pkg.get("version", "")
            repo = pkg.get("repository", "")
            installed = bool(pkg.get("installed", False))
            key = self._pkg_row_icon_key(pkg)
            icon = self.icon_manager.get_package_icon(key, PACKAGE_LIST_ICON_SIZE)
            if not icon:
                icon = self.icon_manager.get_better_fallback_icon(key, PACKAGE_LIST_ICON_SIZE)
            self.store.append(
                [icon, name, version, pkg.get("description", ""), repo, installed]
            )
        self.count_label.set_text(f"{len(packages)} packages")
        # rebuild Repositories tree
        if hasattr(self, 'repo_store'):
            self.repo_store.clear()
            groups = {}
            for p in packages:
                r = p.get('repository', 'unknown')
                groups.setdefault(r, []).append(p.get('name', ''))
            for r, names in sorted(groups.items()):
                parent = self.repo_store.append(None, [r, r, False])
                for n in sorted(names):
                    self.repo_store.append(parent, [n, n, True])

    def on_selection_changed(self, selection):
        model, itr = selection.get_selected()
        if itr is None:
            self.install_btn.set_sensitive(False)
            self.remove_btn.set_sensitive(False)
            self.reinstall_btn.set_sensitive(False)
            if hasattr(self, "details_install_btn"):
                self.details_install_btn.set_sensitive(False)
                self.details_remove_btn.set_sensitive(False)
            return
        installed = model[itr][5]
        self.install_btn.set_sensitive(not installed)
        self.remove_btn.set_sensitive(installed)
        self.reinstall_btn.set_sensitive(installed)
        name = model[itr][1]
        repo = model[itr][4]
        self.fetch_and_show_details(name, repo)
        if hasattr(self, "details_install_btn"):
            self.details_install_btn.set_sensitive(not installed)
            self.details_remove_btn.set_sensitive(installed)

    def on_row_activated(self, tree, path, column):
        # no-op, selection change already handled
        pass

    # Details logic
    def fetch_and_show_details(self, name, repo):
        if hasattr(self, 'details_worker') and self.details_worker and self.details_worker.is_alive():
            return
        def worker():
            details = self._get_package_details(name, repo, with_deps=False)
            GLib.idle_add(self._apply_details, details)
        self.details_worker = threading.Thread(target=worker, daemon=True)
        self.details_worker.start()

    def _get_package_details(self, name, repo, with_deps=False):
        info = {
            'Name': name,
            'Version': '-',
            'Repository': repo,
            'Installed': 'No',
            'Installed Size': '-',
            'License': '-',
            'URL': '-',
            'Description': '-',
            'Depends': [],
            'RequiredBy': [],
        }
        # cache first
        cache_key = f"{repo}:{name}:{'deps' if with_deps else 'nodeps'}"
        if cache_key in self.details_cache:
            return self.details_cache[cache_key]
        stem = name.split("/")[-1] if "/" in name else name
        try:
            qi = subprocess.run(["pacman", "-Qi", stem], capture_output=True, text=True, timeout=5)
            if qi.returncode == 0:
                info['Installed'] = 'Yes'
                self._parse_key_values(qi.stdout, info)
            else:
                if repo == "aur":
                    helper = _aur_helper_cmd()
                    si = None
                    if helper:
                        si = subprocess.run(
                            [helper, "-Si", stem], capture_output=True, text=True, timeout=8
                        )
                    if si is not None and si.returncode == 0:
                        self._parse_key_values(si.stdout, info)
                else:
                    si_arg = name if "/" in name else stem
                    si = subprocess.run(
                        ["pacman", "-Si", si_arg], capture_output=True, text=True, timeout=8
                    )
                    if si.returncode == 0:
                        self._parse_key_values(si.stdout, info)
        except Exception:
            pass
        if with_deps:
            try:
                dep = subprocess.run(["pactree", stem], capture_output=True, text=True, timeout=5)
                if dep.returncode == 0:
                    deps = [l.strip() for l in dep.stdout.split('\n') if l.strip()]
                    info['Depends'] = deps[:50]
            except Exception:
                pass
            try:
                rdep = subprocess.run(["pactree", "-r", stem], capture_output=True, text=True, timeout=5)
                if rdep.returncode == 0:
                    rdeps = [l.strip() for l in rdep.stdout.split('\n') if l.strip()]
                    info['RequiredBy'] = rdeps[:50]
            except Exception:
                pass
        self.details_cache[cache_key] = info
        # keep cache size bounded (LRU-like simple cull)
        if len(self.details_cache) > 200:
            for k in list(self.details_cache.keys())[:50]:
                self.details_cache.pop(k, None)
        return info

    def _parse_key_values(self, text, info):
        for line in text.split('\n'):
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip(); val = val.strip()
                kl = key.lower()
                if kl == 'version':
                    info['Version'] = val
                elif kl in ('repository', 'repo'):
                    info['Repository'] = val
                elif kl == 'installed size':
                    info['Installed Size'] = val
                elif kl == 'license':
                    info['License'] = val
                elif kl == 'url':
                    info['URL'] = val
                elif kl == 'description':
                    info['Description'] = val
                elif kl in ('depends on', 'depends'):
                    info['Depends'] = [p.strip() for p in val.replace('None', '').split() if p.strip()]

    def _apply_details(self, d):
        if not hasattr(self, 'd_name'):
            return
        self.d_name.set_text(d.get('Name', '-'))
        self.d_version.set_text(d.get('Version', '-'))
        self.d_repo.set_text(d.get('Repository', '-'))
        self.d_installed.set_text(d.get('Installed', 'No'))
        self.d_size.set_text(d.get('Installed Size', '-'))
        self.d_license.set_text(d.get('License', '-'))
        self.d_url.set_text(d.get('URL', '-'))
        self._set_textbuf(self.desc_buf, d.get('Description', '-'))
        self._set_textbuf(self.deps_buf, '\n'.join(d.get('Depends', [])) or '-')
        self._set_textbuf(self.rdeps_buf, '\n'.join(d.get('RequiredBy', [])) or '-')

    def _load_current_deps(self, *_):
        # Load dependencies for the currently selected package in main packages tab
        if not hasattr(self, 'tree'):
            return
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        name = model[itr][1]
        repo = model[itr][4]
        def worker():
            d = self._get_package_details(name, repo, with_deps=True)
            GLib.idle_add(self._apply_details, d)
        threading.Thread(target=worker, daemon=True).start()

    def _set_textbuf(self, buf, text):
        buf.set_text(str(text) if text is not None else '-')

    def _copy_details(self, *_):
        clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        if not hasattr(self, 'd_name'):
            return
        summary = (
            f"Name: {self.d_name.get_text()}\n"
            f"Version: {self.d_version.get_text()}\n"
            f"Repo: {self.d_repo.get_text()}\n"
            f"Installed: {self.d_installed.get_text()}\n"
            f"Size: {self.d_size.get_text()}\n"
            f"License: {self.d_license.get_text()}\n"
            f"URL: {self.d_url.get_text()}\n\n"
            f"Description:\n{self._get_buf_text(self.desc_buf)}\n\n"
            f"Dependencies:\n{self._get_buf_text(self.deps_buf)}\n\n"
            f"Required By:\n{self._get_buf_text(self.rdeps_buf)}\n"
        )
        try:
            clip.set_text(summary, -1)
            self._info("Copied", "Details copied to clipboard.")
        except Exception:
            pass

    def _export_details(self, *_):
        if not hasattr(self, 'd_name'):
            return
        dialog = Gtk.FileChooserDialog(title="Export Details", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name(f"{self.d_name.get_text()}-details.txt")
        resp = dialog.run()
        filename = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(self._get_buf_text_for_export())
            self._info("Exported", "Details exported successfully.")
        except Exception as e:
            self._error("Export Failed", str(e))

    def _open_homepage(self, *_):
        if not hasattr(self, 'd_url'):
            return
        url = self.d_url.get_text().strip()
        if url and url != '-':
            try:
                subprocess.Popen(["xdg-open", url])
            except Exception as e:
                self._error("Open URL Failed", str(e))

    def _get_buf_text(self, buf):
        start, end = buf.get_bounds()
        return buf.get_text(start, end, True)

    def _get_buf_text_for_export(self):
        return (
            f"Name: {self.d_name.get_text()}\n"
            f"Version: {self.d_version.get_text()}\n"
            f"Repository: {self.d_repo.get_text()}\n"
            f"Installed: {self.d_installed.get_text()}\n"
            f"Installed Size: {self.d_size.get_text()}\n"
            f"License: {self.d_license.get_text()}\n"
            f"URL: {self.d_url.get_text()}\n\n"
            f"Description:\n{self._get_buf_text(self.desc_buf)}\n\n"
            f"Dependencies:\n{self._get_buf_text(self.deps_buf)}\n\n"
            f"Required By:\n{self._get_buf_text(self.rdeps_buf)}\n"
        )

    # Repos page behavior
    def _on_repo_selection(self, selection):
        model, itr = selection.get_selected()
        if itr is None:
            if hasattr(self, 'repo_detail'):
                self.repo_detail.set_text("Select a package to view details")
            return
        is_pkg = model[itr][2]
        label = model[itr][0]
        if hasattr(self, 'repo_detail'):
            if is_pkg:
                self.repo_detail.set_text(f"Package: {label}")
            else:
                self.repo_detail.set_text(f"Repository: {label}")

    def _repo_view_in_details(self, *_):
        if not hasattr(self, 'repo_tree'):
            return
        selection = self.repo_tree.get_selection()
        model, itr = selection.get_selected()
        if itr is None or not model[itr][2]:
            return
        full_name = model[itr][1]
        repo = full_name.split('/')[0] if '/' in full_name else 'unknown'
        name = full_name
        if hasattr(self, 'notebook'):
            self.notebook.set_current_page(1)  # Details tab
        self.fetch_and_show_details(name, repo)

    # Actions
    def request_sudo(self):
        if os.geteuid() == 0:
            return True
        dialog = Gtk.Dialog(title="Authentication Required", transient_for=self, flags=0)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)

        label = Gtk.Label(label="PKGER requires administrator privileges to manage packages.\nPlease enter your password:")
        label.set_xalign(0.0)
        box.add(label)

        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_invisible_char('•')
        entry.set_placeholder_text("Enter your password...")
        box.add(entry)

        entry.connect("activate", lambda *_: dialog.response(Gtk.ResponseType.OK))
        dialog.show_all()
        resp = dialog.run()
        pwd = entry.get_text() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not pwd:
            return False
        # test password
        try:
            test_cmd = ["bash", "-lc", f"echo '{pwd}' | sudo -S echo test"]
            r = subprocess.run(test_cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                self.sudo_password = pwd
                return True
        except Exception:
            pass
        self._error("Authentication Failed", "Incorrect password. Please try again.")
        return False

    def browse_local_package(self):
        dialog = Gtk.FileChooserDialog(
            title="Select Local Package File",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        flt = Gtk.FileFilter()
        flt.set_name("Arch Package Files")
        flt.add_pattern("*.pkg.tar.zst")
        flt.add_pattern("*.pkg.tar.xz")
        dialog.add_filter(flt)
        dialog.set_current_folder(os.path.expanduser("~"))

        response = dialog.run()
        filename = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()

        if filename:
            self.install_local_package(filename)

    def install_local_package(self, package_file):
        if not os.path.exists(package_file):
            self._error("File Not Found", f"The selected package file does not exist:\n{package_file}")
            return
        if not (package_file.endswith('.pkg.tar.zst') or package_file.endswith('.pkg.tar.xz')):
            self._error("Invalid File", "Please select a valid Arch package file (.pkg.tar.zst or .pkg.tar.xz)")
            return
        if not self.request_sudo():
            return
        basename = os.path.basename(package_file)
        confirm = self._confirm("Confirm Local Installation", f"Are you sure you want to install the local package:\n\n{basename}\n\nFrom: {package_file}")
        if not confirm:
            return
        cmd = ["pacman", "-U", package_file, "--noconfirm"]
        self.append_output(f"Installing local package: {basename}")
        self.run_package_operation(cmd, f"local package installation ({basename})")

    def install_package(self):
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        name = model[itr][1]
        repo = model[itr][4]
        if repo == "local":
            path = None
            stem = name.split("/")[-1] if "/" in name else name
            for p in self.packages_cache:
                if p.get("repository") == "local" and p.get("file_path"):
                    if p.get("pkg") == stem or p.get("name") == stem:
                        path = p["file_path"]
                        break
            if path and os.path.isfile(path):
                self.install_local_package(path)
            else:
                self._warn("Local package", "Could not resolve file path; use Browse local package.")
            return
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Installation", f"Are you sure you want to install '{name}'?"):
            return
        if repo == "aur":
            h = _aur_helper_cmd()
            if not h:
                self._info("No AUR helper", "Install yay or paru to use AUR.")
                return
            stem = name.split("/")[-1]
            cmd = [h, "-S", stem, "--noconfirm"]
            self.run_package_operation(cmd, "install", use_root_stream=False)
            return
        cmd = ["pacman", "-S", name, "--noconfirm"]
        self.run_package_operation(cmd, "install")

    def remove_package(self):
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        name = model[itr][1]
        stem = name.split("/")[-1] if "/" in name else name
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Removal", f"Are you sure you want to remove '{stem}'?"):
            return
        cmd = ["pacman", "-R", stem, "--noconfirm"]
        self.run_package_operation(cmd, "remove")

    def reinstall_package(self):
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        name = model[itr][1]
        repo = model[itr][4]
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Reinstallation", f"Are you sure you want to reinstall '{name}'?"):
            return
        if repo == "aur":
            h = _aur_helper_cmd()
            if not h:
                self._info("No AUR helper", "Install yay or paru to use AUR.")
                return
            stem = name.split("/")[-1]
            cmd = [h, "-S", stem, "--noconfirm"]
            self.run_package_operation(cmd, "reinstall", use_root_stream=False)
            return
        cmd = ["pacman", "-S", name, "--noconfirm"]
        self.run_package_operation(cmd, "reinstall")

    def update_system(self):
        if not self.request_sudo():
            return
        if not self._confirm("Confirm System Update", "Are you sure you want to update the entire system?"):
            return
        cmd = ["pacman", "-Syu", "--noconfirm"]
        self.run_package_operation(cmd, "system update")

    def clean_cache(self):
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Cache Cleaning", "Are you sure you want to clean the package cache?"):
            return
        cmd = ["pacman", "-Sc", "--noconfirm"]
        self.run_package_operation(cmd, "cache cleaning")

    def fix_broken_dependencies(self):
        if not self.request_sudo():
            return
        if not self._confirm("Fix Broken Dependencies", "This will attempt to fix broken dependencies. Continue?"):
            return
        lock_file = "/var/lib/pacman/db.lck"
        if os.path.exists(lock_file):
            try:
                os.remove(lock_file)
                self.append_output("Removed pacman lock file")
            except Exception:
                pass
        cmd = ["pacman", "-Syy"]
        self.run_package_operation(cmd, "dependency fix")

    def run_package_operation(self, command, operation_type, use_root_stream=True):
        """use_root_stream=False for helpers like yay/paru that must not run as root via sudo -S."""
        if self.current_worker and self.current_worker.is_alive():
            self._warn("Operation in Progress", "Another operation is already running. Please wait.")
            return
        self.textbuf.set_text("")
        self.set_progress_value(0)
        self.set_status(f"Starting {operation_type}...")

        # disable controls during operation
        self._set_controls_sensitive(False)

        def ui_callbacks():
            return {
                'output': self.append_output,
                'progress': self.set_progress_value,
                'status': self.set_status,
                'done': self.on_operation_finished,
            }

        pwd = self.sudo_password if use_root_stream else None
        self.current_worker = PackageOpWorker(command, operation_type, pwd, ui_callbacks())
        self.current_worker.start()

    def on_operation_finished(self, success, message):
        self.set_progress_value(0)
        self.set_status("Ready")
        # enable controls
        self._set_controls_sensitive(True)
        if success:
            self._info("Success", message)
            if self.search_type.get_active_text() == "Installed":
                self.load_installed_packages()
            self._refresh_system_stats()
            self.load_updates_data()
        else:
            self._error("Error", message)

    def refresh_packages(self):
        query = self.search_entry.get_text().strip()
        if query:
            self.search_packages()
        else:
            self.load_installed_packages()

    def refresh_all(self):
        # Global refresh: packages list, repos catalog, and details (if any selection)
        self.load_installed_packages()
        self.load_repos_data()
        # Refresh repo packages according to current selection
        if hasattr(self, 'repos_view'):
            self._filter_repos_tree()
        # Refresh updates
        self.load_updates_data()
        self._refresh_system_stats()

    # ----------------------------
    # System tools (Arch integration)
    # ----------------------------
    def _refresh_system_stats(self, *_):
        if not hasattr(self, "system_stats_label"):
            return

        def worker():
            parts = []
            try:
                r = subprocess.run(
                    ["du", "-sh", "/var/cache/pacman/pkg"],
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                if r.returncode == 0 and r.stdout.strip():
                    parts.append(f"Package cache: {r.stdout.split()[0]}")
            except Exception:
                parts.append("Package cache: (unavailable)")
            try:
                r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=15)
                if r.returncode == 0:
                    ln = r.stdout.strip().split("\n")
                    if len(ln) >= 2:
                        parts.append("Root: " + " ".join(ln[1].split()))
            except Exception:
                pass
            try:
                r = subprocess.run(["pacman", "-V"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    parts.append(r.stdout.strip().split("\n")[0])
            except Exception:
                pass
            try:
                r = subprocess.run(["pacman", "-Qu"], capture_output=True, text=True, timeout=90)
                n = 0
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    sp = line.split()
                    if len(sp) >= 4 and sp[2] == "->":
                        n += 1
                parts.append(f"Pending official updates: {n}")
            except Exception:
                pass
            parts.append(
                "Helpers: yay=%s, paru=%s, reflector=%s"
                % (
                    "yes" if command_exists("yay") else "no",
                    "yes" if command_exists("paru") else "no",
                    "yes" if command_exists("reflector") else "no",
                )
            )
            GLib.idle_add(self.system_stats_label.set_text, "\n".join(parts))

        threading.Thread(target=worker, daemon=True).start()

    def _tool_sync_databases(self, *_):
        if not self.request_sudo():
            return
        if not self._confirm(
            "Sync databases",
            "Run pacman -Sy to refresh sync databases (no package upgrade)?",
        ):
            return
        self.run_package_operation(["pacman", "-Sy", "--noconfirm"], "sync databases")

    def _tool_update_official(self, *_):
        self.update_system()

    def _tool_update_full_aur(self, *_):
        if command_exists("yay"):
            cmd = ["yay", "-Syu", "--noconfirm"]
        elif command_exists("paru"):
            cmd = ["paru", "-Syu", "--noconfirm"]
        else:
            self._info(
                "No AUR helper",
                "Install yay or paru to update the AUR together with the system.",
            )
            return
        if not self._confirm(
            "Full update",
            "Run a full system update including AUR with --noconfirm?\n"
            "(yay/paru will ask for privilege escalation when needed.)",
        ):
            return
        self.run_package_operation(cmd, "full update (AUR)", use_root_stream=False)

    def _tool_deep_clean_cache(self, *_):
        if not self.request_sudo():
            return
        if not self._confirm(
            "Deep clean cache",
            "Remove all cached package files (pacman -Scc)?",
        ):
            return
        self.run_package_operation(["pacman", "-Scc", "--noconfirm"], "deep clean cache")

    def _tool_refresh_keyring(self, *_):
        if not self.request_sudo():
            return
        if not self._confirm(
            "Refresh keyring",
            "Run pacman-key --refresh-keys? Requires network access.",
        ):
            return
        self.run_package_operation(["pacman-key", "--refresh-keys"], "refresh keyring")

    def _tool_verify_files(self, *_):
        if not self._confirm(
            "Verify files",
            "Run pacman -Qk to verify installed files against package database? May take time.",
        ):
            return
        self.set_status("Verifying installed files…")

        def worker():
            try:
                r = subprocess.run(["pacman", "-Qk"], capture_output=True, text=True, timeout=600)
                out = (r.stdout or "") + (r.stderr or "")
                if len(out) > 12000:
                    out = out[:12000] + "\n… (truncated)"
                GLib.idle_add(self.append_output, "=== pacman -Qk ===\n" + out)
            except Exception as e:
                GLib.idle_add(self.append_output, f"Verify failed: {e}")
            GLib.idle_add(self.set_status, "Ready")

        threading.Thread(target=worker, daemon=True).start()

    def _tool_check_database(self, *_):
        self.set_status("Checking database…")

        def worker():
            try:
                r = subprocess.run(["pacman", "-Dk"], capture_output=True, text=True, timeout=120)
                out = (r.stdout or "") + (r.stderr or "")
                if len(out) > 12000:
                    out = out[:12000] + "\n… (truncated)"
                GLib.idle_add(self.append_output, "=== pacman -Dk ===\n" + out)
            except Exception as e:
                GLib.idle_add(self.append_output, f"Database check failed: {e}")
            GLib.idle_add(self.set_status, "Ready")

        threading.Thread(target=worker, daemon=True).start()

    def _tool_reflector(self, *_):
        if not command_exists("reflector"):
            self._warn("reflector not found", "Install with: pacman -S reflector")
            return
        if not self.request_sudo():
            return
        if not self._confirm(
            "Reflector",
            "Overwrite /etc/pacman.d/mirrorlist with the latest 20 mirrors (sorted by rate)?",
        ):
            return
        self.run_package_operation(
            [
                "reflector",
                "--latest",
                "20",
                "--sort",
                "rate",
                "--save",
                "/etc/pacman.d/mirrorlist",
            ],
            "reflector mirrors",
        )

    def _tool_export_explicit(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Save explicit package list",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,
            Gtk.ResponseType.OK,
        )
        dialog.set_current_name("explicit-packages.txt")
        resp = dialog.run()
        filename = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return

        def worker():
            try:
                r = subprocess.run(["pacman", "-Qqe"], capture_output=True, text=True, timeout=120)
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(r.stdout)
                GLib.idle_add(self._info, "Exported", f"Explicit packages saved to:\n{filename}")
            except Exception as e:
                GLib.idle_add(self._error, "Export failed", str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _tool_reload_pacman_log(self, *_):
        if not hasattr(self, "pacman_log_buf"):
            return
        path = "/var/log/pacman.log"

        def worker():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                tail = "".join(lines[-500:])
                GLib.idle_add(self.pacman_log_buf.set_text, tail)
            except Exception as e:
                GLib.idle_add(
                    self.pacman_log_buf.set_text,
                    f"Cannot read {path}: {e}\n"
                    "(You may need read access to the log or run related checks as root.)",
                )

        threading.Thread(target=worker, daemon=True).start()

    def _tool_search_file_repos(self, *_):
        q = self.file_search_entry.get_text().strip()
        if not q:
            return

        def worker():
            try:
                r = subprocess.run(["pacman", "-Fs", q], capture_output=True, text=True, timeout=90)
                out = (r.stdout or "") + (r.stderr or "")
                if not out.strip():
                    out = (
                        "(no results — ensure file DB is synced: sudo pacman -Fy)\n"
                    )
                GLib.idle_add(self.file_tools_buf.set_text, out)
            except Exception as e:
                GLib.idle_add(self.file_tools_buf.set_text, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _tool_query_owns(self, *_):
        path = self.file_owns_entry.get_text().strip()
        if not path:
            return

        def worker():
            try:
                r = subprocess.run(["pacman", "-Qo", path], capture_output=True, text=True, timeout=30)
                out = (r.stdout or "") + (r.stderr or "")
                GLib.idle_add(self.file_tools_buf.set_text, out)
            except Exception as e:
                GLib.idle_add(self.file_tools_buf.set_text, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _tool_load_arch_news(self, *_):
        self.set_status("Fetching Arch Linux news…")

        def worker():
            items = fetch_arch_news_items(20)
            if not items:
                GLib.idle_add(
                    self.arch_news_buf.set_text,
                    "Could not load the news feed. Check your network connection.",
                )
                GLib.idle_add(self.set_status, "Ready")
                return
            lines = []
            for it in items:
                lines.append(f"■ {it['title']}\n  {it['pub_date']}\n  {it['link']}\n")
                if it["summary"]:
                    s = re.sub(r"<[^>]+>", "", it["summary"]).replace("\n", " ")
                    if len(s) > 600:
                        s = s[:600] + "…"
                    lines.append(f"  {s}\n")
                lines.append("\n")
            GLib.idle_add(self.arch_news_buf.set_text, "".join(lines))
            GLib.idle_add(self.set_status, "Ready")

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------------
    # Updates features (like Octopi/Pamac)
    # ----------------------------
    def load_updates_data(self):
        if getattr(self, 'updates_worker', None) and self.updates_worker.is_alive():
            return
        def worker():
            items = []
            try:
                # pacman -Qu lists pending updates (installed -> newer available)
                r = subprocess.run(["pacman", "-Qu"], capture_output=True, text=True, timeout=60)
                for line in r.stdout.strip().split('\n'):
                    if not line.strip():
                        continue
                    # format: name oldver -> newver
                    parts = line.split()
                    if len(parts) >= 4 and parts[2] == '->':
                        name = parts[0]
                        oldver = parts[1]
                        newver = parts[3]
                        items.append({'name': name, 'from': oldver, 'to': newver})
            except Exception:
                pass
            GLib.idle_add(self._apply_updates_data, items)
        self.updates_worker = threading.Thread(target=worker, daemon=True)
        self.updates_worker.start()

    def _apply_updates_data(self, items):
        self.updates_data = items or []
        if hasattr(self, 'upd_store'):
            self.upd_store.clear()
            for it in self.updates_data:
                icon = self.icon_manager.get_package_icon(it['name'], PACKAGE_LIST_ICON_SIZE)
                if not icon:
                    icon = self.icon_manager.get_better_fallback_icon(it['name'], PACKAGE_LIST_ICON_SIZE)
                self.upd_store.append(
                    [False, icon, it['name'], f"{it['from']} -> {it['to']}"]
                )
        # Update title with a badge-like count (simple)
        if hasattr(self, "notebook"):
            idx = 3  # Updates tab index (Operations, Details, Repos, Updates)
            if idx < self.notebook.get_n_pages():
                page = self.notebook.get_nth_page(idx)
                lab = Gtk.Label(label=f"Updates ({len(self.updates_data)})")
                lab.set_tooltip_text(
                    "Pending pacman -Qu upgrades: tick rows, apply, or remove orphan packages."
                )
                lab.show_all()
                self.notebook.set_tab_label(page, lab)

    def _updates_toggle_all(self, btn):
        if not hasattr(self, 'upd_store'):
            return
        desired = btn.get_active()
        it = self.upd_store.get_iter_first()
        while it:
            self.upd_store[it][0] = desired
            it = self.upd_store.iter_next(it)

    def _updates_toggle_row(self, cell, path):
        if not hasattr(self, 'upd_store'):
            return
        it = self.upd_store.get_iter(path)
        self.upd_store[it][0] = not self.upd_store[it][0]

    def _updates_apply_selected(self, *_):
        if not hasattr(self, 'upd_store'):
            return
        to_update = []
        it = self.upd_store.get_iter_first()
        while it:
            if self.upd_store[it][0]:
                to_update.append(self.upd_store[it][2])
            it = self.upd_store.iter_next(it)
        if not to_update:
            return
        if not self.request_sudo():
            return
        cmd = ["pacman", "-S", "--noconfirm"] + to_update
        self.run_package_operation(cmd, "apply updates")

    def _remove_orphans(self, *_):
        if not self.request_sudo():
            return
        if not self._confirm(
            "Remove orphans",
            "Remove packages that were installed as dependencies but are no longer required?",
        ):
            return
        self.run_package_operation(
            ["bash", "-c", "pacman -Qtdq | pacman -Rns --noconfirm -"],
            "remove orphans",
        )

    def show_about(self):
        about = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=f"{APP_NAME} {APP_VERSION}",
        )
        about.format_secondary_text(
            "Professional package manager for Arch Linux and derivatives.\n"
            "Repositories browser, updates, system maintenance, file search, pacman log, "
            "Arch news RSS, AUR helpers (yay/paru), package icons, and combined search.\n\n"
            f"Developer: {APP_DEVELOPER}\nYear: {APP_YEAR}"
        )
        about.run()
        about.destroy()

    # Core data functions
    def load_installed_packages(self):
        self.append_output("Loading installed packages...")
        self.set_status("Loading packages...")
        def worker():
            try:
                r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=30)
                packages = []
                versions = {}
                for line in r.stdout.strip().split('\n'):
                    if line.strip():
                        parts = line.split(' ', 1)
                        if len(parts) >= 2:
                            name = parts[0]; ver = parts[1]
                            versions[name] = ver
                            packages.append({
                                'name': name,
                                'version': ver,
                                'description': '',
                                'repository': 'installed',
                                'installed': True
                            })
                GLib.idle_add(self._apply_installed_packages, packages, versions)
            except subprocess.TimeoutExpired:
                GLib.idle_add(self.append_output, "Timeout loading packages")
                GLib.idle_add(self.set_status, "Timeout")
            except Exception as e:
                GLib.idle_add(self.append_output, f"Error loading packages: {e}")
                GLib.idle_add(self.set_status, "Error")
        threading.Thread(target=worker, daemon=True).start()

    def _apply_installed_packages(self, packages, versions):
        self.installed_versions = versions or {}
        self.update_package_list(packages or [])
        self.append_output(f"Loaded {len(packages or [])} installed packages")
        self.set_status("Ready")

    def search_packages(self):
        query = self.search_entry.get_text().strip()
        if not query:
            return
        mapping = {
            "Official": "official",
            "AUR": "aur",
            "Installed": "installed",
            "All (official + AUR)": "all",
        }
        stype = mapping.get(self.search_type.get_active_text(), "official")
        if self.search_worker and self.search_worker.is_alive():
            # no direct cancel; let it finish
            pass
        self.append_output(f"Searching {stype} packages for: {query}")
        self.set_status(f"Searching {stype} packages...")
        self.search_worker = SearchWorker(
            query, stype, self.on_search_results, self.icon_manager
        )
        self.search_worker.start()

    def on_search_results(self, packages):
        self.update_package_list(packages)
        self.append_output(f"Found {len(packages)} packages")
        self.set_status("Search completed")

    # ----------------------------
    # Repositories data loading and actions
    # ----------------------------
    def load_repos_data(self):
        if getattr(self, 'repos_worker', None) and self.repos_worker.is_alive():
            return
        def worker():
            data = {}
            allow = get_pacman_repo_allowlist()
            try:
                # Direct pacman (no shell): avoids profile/motd or other junk on stdout.
                # Format: repo pkgname epoch:pkgver-pkgrel  (epoch optional); may end with [installed]
                r = subprocess.run(
                    ["pacman", "-Sl"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if r.returncode != 0:
                    GLib.idle_add(self._apply_repos_data, data)
                    return
                for raw in r.stdout.splitlines():
                    line = raw.strip()
                    if not line:
                        continue
                    if line.startswith("::"):
                        continue
                    low = line.lower()
                    if low.startswith(("warning:", "error:", "note:")):
                        continue
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    repo = parts[0]
                    if allow is not None:
                        if repo not in allow:
                            continue
                    elif not _is_plausible_repo_name(repo):
                        continue
                    name = parts[1]
                    installed = "[installed]" in line
                    data.setdefault(repo, []).append({"name": name, "installed": installed})
            except Exception:
                pass
            GLib.idle_add(self._apply_repos_data, data)
        self.repos_worker = threading.Thread(target=worker, daemon=True)
        self.repos_worker.start()

    def _repos_list_folder_icon(self):
        """Single pixbuf reused for every row in the left-hand repository list."""
        if not hasattr(self, "_repos_list_folder_pixbuf"):
            p = self.icon_manager._load_icon_by_name("folder-symbolic", PACKAGE_LIST_ICON_SIZE)
            if not p:
                p = self.icon_manager._load_icon_by_name("folder", PACKAGE_LIST_ICON_SIZE)
            if not p:
                p = self.icon_manager.get_package_icon("folder", PACKAGE_LIST_ICON_SIZE)
            self._repos_list_folder_pixbuf = p
        return self._repos_list_folder_pixbuf

    def _apply_repos_data(self, data):
        self.repos_data = data or {}
        # Fill vertical repos list and right table
        if hasattr(self, 'repos_list'):
            self.repos_list.clear()
            folder_icon = self._repos_list_folder_icon()
            for repo in sorted(self.repos_data.keys()):
                self.repos_list.append([folder_icon, repo])
        # Default select first repo
        if hasattr(self, 'repos_view'):
            sel = self.repos_view.get_selection()
            if self.repos_list and len(self.repos_list) > 0:
                itr = self.repos_list.get_iter_first()
                sel.select_iter(itr)
                self._populate_repo_packages(self.repos_list[itr][1])

    def _filter_repos_tree(self):
        # Refresh right table according to current repo and filters
        if not hasattr(self, 'repos_view'):
            return
        model, itr = self.repos_view.get_selection().get_selected()
        if itr is None:
            return
        repo = model[itr][1]
        self._populate_repo_packages(repo)

    def _populate_repo_packages(self, repo):
        if not hasattr(self, 'repo_pkgs_store'):
            return
        self.repo_pkgs_store.clear()
        items = self.repos_data.get(repo, [])
        query = self.repo_search.get_text().strip().lower() if hasattr(self, 'repo_search') else ''
        installed_only = self.repo_filter_installed.get_active() if hasattr(self, 'repo_filter_installed') else False
        # Limit rows for performance unless Show All is active
        limit = None if (hasattr(self, 'repo_show_all') and self.repo_show_all.get_active()) else 200
        count = 0
        for it in sorted(items, key=lambda x: x['name']):
            if installed_only and not it['installed']:
                continue
            if query and query not in it['name'].lower():
                continue
            icon = self.icon_manager.get_package_icon(it['name'], PACKAGE_LIST_ICON_SIZE)
            if not icon:
                icon = self.icon_manager.get_better_fallback_icon(it['name'], PACKAGE_LIST_ICON_SIZE)
            self.repo_pkgs_store.append([False, icon, it['name'], it['installed']])
            count += 1
            if limit is not None and count >= limit:
                break

    def _on_repo_selected(self, selection):
        model, itr = selection.get_selected()
        if itr is None:
            return
        repo = model[itr][1]
        self._populate_repo_packages(repo)

    def _on_repo_pkg_toggled(self, cell, path):
        if not hasattr(self, 'repo_pkgs_store'):
            return
        itr = self.repo_pkgs_store.get_iter(path)
        current = self.repo_pkgs_store[itr][0]
        self.repo_pkgs_store[itr][0] = not current

    def _on_repo_pkg_selection(self, selection):
        model, itr = selection.get_selected()
        if itr is None:
            return
        # get current repo context
        repo_model, repo_itr = self.repos_view.get_selection().get_selected() if hasattr(self, 'repos_view') else (None, None)
        repo = repo_model[repo_itr][1] if repo_model and repo_itr else 'unknown'
        name = model[itr][2]
        self._fetch_and_show_repo_details(repo, name)

    def _fetch_and_show_repo_details(self, repo, name):
        def worker():
            d = self._get_package_details(name, repo)
            GLib.idle_add(self._apply_repo_details, d)
        threading.Thread(target=worker, daemon=True).start()

    def _apply_repo_details(self, d):
        if not hasattr(self, 'repo_d_name'):
            return
        self.repo_d_name.set_text(f"Name: {d.get('Name','-')}")
        self.repo_d_version.set_text(f"Version: {d.get('Version','-')}")
        self.repo_d_repo.set_text(f"Repository: {d.get('Repository','-')}")
        self.repo_d_installed.set_text(f"Installed: {d.get('Installed','No')}")
        self.repo_d_url.set_text(f"URL: {d.get('URL','-')}")
        self.repo_desc_buf.set_text(str(d.get('Description', '-')))

    def _repos_get_selected_packages(self):
        # Use checkbox column state
        pkgs = []
        if not hasattr(self, 'repo_pkgs_store') or not hasattr(self, 'repos_view'):
            return pkgs
        model, itr = self.repos_view.get_selection().get_selected()
        repo = model[itr][1] if itr else None
        if not repo:
            return pkgs
        it = self.repo_pkgs_store.get_iter_first()
        while it:
            if self.repo_pkgs_store[it][0]:
                name = self.repo_pkgs_store[it][2]
                pkgs.append(f"{repo}/{name}")
            it = self.repo_pkgs_store.iter_next(it)
        return pkgs

    def _repos_install_selected(self, *_):
        pkgs = self._repos_get_selected_packages()
        if not pkgs:
            return
        if not self.request_sudo():
            return
        # pacman accepts repo/name; install in one command
        cmd = ["pacman", "-S", "--noconfirm"] + pkgs
        self.run_package_operation(cmd, "install (repos)")

    def _repos_remove_selected(self, *_):
        pkgs = self._repos_get_selected_packages()
        if not pkgs:
            return
        names = [p.split('/')[-1] for p in pkgs]
        if not self.request_sudo():
            return
        cmd = ["pacman", "-R", "--noconfirm"] + names
        self.run_package_operation(cmd, "remove (repos)")

    def _repos_export_list(self, *_):
        dialog = Gtk.FileChooserDialog(title="Export Repositories List", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("repositories-list.txt")
        resp = dialog.run()
        filename = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                for repo, items in sorted(self.repos_data.items()):
                    f.write(f"[{repo}]\n")
                    for it in sorted(items, key=lambda x: x['name']):
                        flag = '*' if it['installed'] else ' '
                        f.write(f" {flag} {it['name']}\n")
                    f.write("\n")
            self._info("Exported", "Repositories list exported successfully.")
        except Exception as e:
            self._error("Export Failed", str(e))

    # UI helpers
    def _set_controls_sensitive(self, enabled):
        self.update_btn.set_sensitive(enabled)
        self.clean_btn.set_sensitive(enabled)
        self.browse_btn.set_sensitive(enabled)
        self.refresh_all_btn.set_sensitive(enabled)
        if hasattr(self, "reset_sort_btn"):
            self.reset_sort_btn.set_sensitive(enabled)
        self.search_btn.set_sensitive(enabled)
        self.search_entry.set_sensitive(enabled)
        self.search_type.set_sensitive(enabled)
        if hasattr(self, "upd_refresh_btn"):
            self.upd_refresh_btn.set_sensitive(enabled)
            self.upd_apply_btn.set_sensitive(enabled)
            self.orphans_btn.set_sensitive(enabled)
            self.upd_select_all.set_sensitive(enabled)
        if hasattr(self, "repo_install_btn"):
            self.repo_install_btn.set_sensitive(enabled)
            self.repo_remove_btn.set_sensitive(enabled)
            self.repo_export_btn.set_sensitive(enabled)
            self.repo_search.set_sensitive(enabled)
            self.repo_filter_installed.set_sensitive(enabled)
            self.repo_show_all.set_sensitive(enabled)
        for b in getattr(self, "_system_tool_buttons", ()):
            b.set_sensitive(enabled)
        if hasattr(self, "file_search_entry"):
            self.file_search_entry.set_sensitive(enabled)
            self.file_owns_entry.set_sensitive(enabled)
        if not enabled:
            self.install_btn.set_sensitive(False)
            self.remove_btn.set_sensitive(False)
            self.reinstall_btn.set_sensitive(False)
        else:
            model, itr = self.tree.get_selection().get_selected()
            if itr is None:
                self.install_btn.set_sensitive(False)
                self.remove_btn.set_sensitive(False)
                self.reinstall_btn.set_sensitive(False)
            else:
                installed = model[itr][5]
                self.install_btn.set_sensitive(not installed)
                self.remove_btn.set_sensitive(installed)
                self.reinstall_btn.set_sensitive(installed)
                if hasattr(self, "details_install_btn"):
                    self.details_install_btn.set_sensitive(not installed)
                    self.details_remove_btn.set_sensitive(installed)

    def reset_sorting(self, *_):
        """Rebuild the sidebar list from ``packages_cache`` (original cached order)."""
        if self.packages_cache is not None:
            self.store.set_sort_column_id(
                Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID, Gtk.SortType.ASCENDING
            )
            self.update_package_list(list(self.packages_cache))
            self.append_output("Package list order reset.")

    def _export_package_list(self):
        dialog = Gtk.FileChooserDialog(
            title="Export Package List", parent=self, action=Gtk.FileChooserAction.SAVE
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK
        )
        dialog.set_current_name("package-list.txt")
        resp = dialog.run()
        filename = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write("PKGER Package List Export\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total: {len(self.packages_cache)}\n\n")
                for pkg in self.packages_cache:
                    if isinstance(pkg, PackageInfo):
                        f.write(f"{pkg.name} - {pkg.version} ({pkg.repository})\n")
                    else:
                        f.write(
                            f"{pkg.get('name', '')} - {pkg.get('version', '')} ({pkg.get('repository', '')})\n"
                        )
            self._info("Exported", "Package list exported successfully.")
        except OSError as e:
            self._error("Export Failed", str(e))

    def _show_system_info(self):
        try:
            arch = os.uname().machine if hasattr(os, "uname") else "unknown"
            user = os.getenv("USER", "unknown")
            hostname = os.uname().nodename if hasattr(os, "uname") else "unknown"
            try:
                result = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=5)
                package_count = (
                    len([x for x in result.stdout.strip().split("\n") if x.strip()])
                    if result.stdout.strip()
                    else 0
                )
            except Exception:
                package_count = 0
            info_text = (
                f"Hostname: {hostname}\n"
                f"User: {user}\n"
                f"Architecture: {arch}\n"
                f"Installed packages: {package_count}\n"
                f"PKGER: {APP_VERSION}\n"
                f"Developer: {APP_DEVELOPER} · {APP_YEAR}"
            )
            d = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="System Information",
            )
            d.format_secondary_text(info_text)
            d.run()
            d.destroy()
        except Exception as e:
            self._error("Error", str(e))

    def _show_package_stats(self):
        try:
            repo_stats = {}
            installed_count = 0
            for pkg in self.packages_cache:
                if isinstance(pkg, PackageInfo):
                    repo = pkg.repository
                    inst = pkg.installed
                else:
                    repo = pkg.get("repository", "unknown")
                    inst = pkg.get("installed", False)
                repo_stats[repo] = repo_stats.get(repo, 0) + 1
                if inst:
                    installed_count += 1
            lines = [
                f"Total in list: {len(self.packages_cache)}",
                f"Marked installed: {installed_count}",
                "",
                "By repository:",
            ]
            for repo, count in sorted(repo_stats.items()):
                lines.append(f"  • {repo}: {count}")
            d = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Package statistics",
            )
            d.format_secondary_text("\n".join(lines))
            d.run()
            d.destroy()
        except Exception as e:
            self._error("Error", str(e))

    def _show_user_guide(self):
        guide = (
            "Search: Official, AUR, Installed, or All (official + AUR).\n"
            "Select a package → Operations (install/remove/reinstall) or Details tab.\n"
            "Repositories tab: filter pacman -Sl catalog, batch install/remove.\n"
            "Updates tab: selective pacman -S; System tab: maintenance tools.\n"
            "Use Help → System information for a quick host summary."
        )
        d = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="PKGER — User guide",
        )
        d.format_secondary_text(guide)
        d.run()
        d.destroy()

    def _show_keyboard_shortcuts(self):
        txt = (
            "Ctrl+Q — quit (from menu)\n"
            "Enter in search — run search\n"
            "Tab — move focus; arrows in lists\n"
            "Double-click rows follows GTK defaults"
        )
        d = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Keyboard shortcuts",
        )
        d.format_secondary_text(txt)
        d.run()
        d.destroy()

    def _search_local_packages_dialog(self):
        dlg = Gtk.Dialog(title="Search local package files", transient_for=self, flags=0)
        dlg.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        dlg.add_button(Gtk.STOCK_OK, Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(8)
        lbl = Gtk.Label(label="Substring to match in .pkg.tar.zst / .pkg.tar.xz filenames:")
        lbl.set_xalign(0.0)
        box.add(lbl)
        entry = Gtk.Entry()
        entry.set_placeholder_text("e.g. linux")
        box.add(entry)
        dlg.show_all()
        resp = dlg.run()
        q = entry.get_text().strip()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK or not q:
            return
        pkgs = self._search_local_packages(q)
        self.update_package_list(pkgs)
        self.append_output(f"Local file search: {len(pkgs)} match(es) for {q!r}")

    def _search_local_packages(self, query):
        packages = []
        local_dirs = [
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/"),
            "/tmp",
            "/var/cache/pacman/pkg",
        ]
        qlow = query.lower()
        for directory in local_dirs:
            if not os.path.isdir(directory):
                continue
            try:
                for filename in os.listdir(directory):
                    if not filename.endswith((".pkg.tar.zst", ".pkg.tar.xz")):
                        continue
                    if qlow not in filename.lower():
                        continue
                    parts = filename.split("-")
                    stem = parts[0] if parts else filename
                    ver_guess = "-".join(parts[1:-2]) if len(parts) > 2 else ""
                    packages.append(
                        {
                            "name": stem,
                            "pkg": stem,
                            "version": ver_guess,
                            "description": f"Local file: {filename}",
                            "repository": "local",
                            "installed": False,
                            "file_path": os.path.join(directory, filename),
                        }
                    )
            except OSError:
                continue
        return packages

    def _install_from_details_tab(self, *_):
        name = self.d_name.get_text().strip() if hasattr(self, "d_name") else ""
        if not name or name == "-":
            return
        repo_txt = self.d_repo.get_text().strip() if hasattr(self, "d_repo") else ""
        repo = "unknown"
        if ":" in repo_txt:
            repo = repo_txt.split(":", 1)[1].strip().lower()
        self.install_package_by_name(name, repo)

    def _remove_from_details_tab(self, *_):
        name = self.d_name.get_text().strip() if hasattr(self, "d_name") else ""
        if not name or name == "-":
            return
        stem = name.split("/")[-1] if "/" in name else name
        self.remove_package_by_name(stem)

    def install_package_by_name(self, name, repo="unknown"):
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Installation", f"Install '{name}'?"):
            return
        if repo == "aur":
            h = _aur_helper_cmd()
            if not h:
                self._info("No AUR helper", "Install yay or paru.")
                return
            stem = name.split("/")[-1]
            self.run_package_operation([h, "-S", stem, "--noconfirm"], "install", use_root_stream=False)
            return
        self.run_package_operation(["pacman", "-S", name, "--noconfirm"], "install")

    def remove_package_by_name(self, name):
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Removal", f"Remove '{name}'?"):
            return
        self.run_package_operation(["pacman", "-R", name, "--noconfirm"], "remove")

    def _confirm(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=title)
        d.format_secondary_text(text)
        resp = d.run()
        d.destroy()
        return resp == Gtk.ResponseType.YES

    def _info(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def _warn(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def _error(self, title, text):
        d = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()


def main():
    if not command_exists('pacman'):
        print("Warning: pacman not found in PATH. Some features will not work.")
    app = PKGERWindow()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
