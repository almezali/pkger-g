#!/usr/bin/env python3
"""
PKGER — desktop package manager for Arch Linux and Arch-based systems.

Single-file application providing a GTK 4 front end around pacman, optional AUR
helpers (yay, paru), Flatpak discovery, AppImage scanning, repository browsing,
updates, system maintenance helpers, optional tray integration, and Arch Linux
news via RSS.

Environment:
    PKGER_DEBUG=1 — enable debug logging to stderr for troubleshooting.

Command line:
    pkger.py [--version] | [--help]
"""

import argparse
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

try:
    import gi
except Exception:
    print("PyGObject is required. Install it with: pacman -S python-gobject")
    sys.exit(1)

def _maybe_add_gi_typelib_path():
    """Best-effort: make GI find bundled typelibs (useful for Nuitka onefile)."""
    is_nuitka = "__compiled__" in globals()
    debug = (os.environ.get("PKGER_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")
    
    if debug:
        print(f"DEBUG: is_nuitka={is_nuitka}", file=sys.stderr)
        print(f"DEBUG: __file__={__file__}", file=sys.stderr)
        print(f"DEBUG: sys.argv[0]={sys.argv[0]}", file=sys.stderr)

    try:
        # In Nuitka standalone/onefile, __file__ points to the unpacked location.
        bundle_dir = Path(__file__).resolve().parent
        # sys.argv[0] might be the launcher or the unpacked binary.
        here = Path(sys.argv[0]).resolve().parent
    except Exception as e:
        if debug: print(f"DEBUG: Error resolving paths: {e}", file=sys.stderr)
        bundle_dir = Path.cwd()
        here = Path.cwd()

    # Typelib search candidates
    candidates = [
        bundle_dir / "girepository-1.0",
        bundle_dir / "gi_typelibs",
        bundle_dir / "lib" / "girepository-1.0",
        bundle_dir / "usr" / "lib" / "girepository-1.0",
        here / "girepository-1.0",
        here / "lib" / "girepository-1.0",
    ]
    
    existing = [str(p) for p in candidates if p.exists()]
    if debug: print(f"DEBUG: Found typelib candidates: {existing}", file=sys.stderr)
    
    if existing:
        current = os.environ.get("GI_TYPELIB_PATH") or ""
        parts = [p for p in current.split(":") if p]
        for p in reversed(existing):
            if p not in parts:
                parts.insert(0, p)
        os.environ["GI_TYPELIB_PATH"] = ":".join(parts)

    # Library search candidates (LD_LIBRARY_PATH)
    lib_candidates = [
        bundle_dir,
        bundle_dir / "lib",
        bundle_dir / "usr" / "lib",
        here,
        here / "lib",
    ]
    existing_libs = [str(p) for p in lib_candidates if p.exists()]
    if debug: print(f"DEBUG: Found lib candidates: {existing_libs}", file=sys.stderr)
    
    if existing_libs:
        current_ld = os.environ.get("LD_LIBRARY_PATH") or ""
        ld_parts = [p for p in current_ld.split(":") if p]
        for p in reversed(existing_libs):
            if p not in ld_parts:
                ld_parts.insert(0, p)
        os.environ["LD_LIBRARY_PATH"] = ":".join(ld_parts)

    # GDK Pixbuf loaders (often needed for icons in standalone)
    pixbuf_loaders = bundle_dir / "lib" / "gdk-pixbuf-2.0" / "2.10.0" / "loaders.cache"
    if pixbuf_loaders.exists():
        if debug: print(f"DEBUG: Setting GDK_PIXBUF_MODULE_FILE to {pixbuf_loaders}", file=sys.stderr)
        os.environ["GDK_PIXBUF_MODULE_FILE"] = str(pixbuf_loaders)
    
    # GSettings schemas (GTK4 might need these for basic functionality)
    schemas_dir = bundle_dir / "share" / "glib-2.0" / "schemas"
    if schemas_dir.exists():
        if debug: print(f"DEBUG: Found schemas at {schemas_dir}", file=sys.stderr)
        os.environ["XDG_DATA_DIRS"] = str(bundle_dir / "share") + ":" + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share")
    
    # Nuitka 4.x/Python 3.14 specific: sometimes sys.path needs adjustment for GI
    if is_nuitka and str(bundle_dir) not in sys.path:
        sys.path.insert(0, str(bundle_dir))
        if debug: print(f"DEBUG: Added {bundle_dir} to sys.path", file=sys.stderr)


GTK4_AVAILABLE = False
Gtk4 = None
Gdk4 = None
Gio4 = None
GObject4 = None
_gtk4_exc = None
try:
    _maybe_add_gi_typelib_path()
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk as Gtk4, Gdk as Gdk4, Gio as Gio4, GObject as GObject4  # type: ignore
    GTK4_AVAILABLE = True
except Exception as _gtk4_exc:
    GTK4_AVAILABLE = False

try:
    from gi.repository import GLib  # type: ignore
except Exception:
    GLib = None

if not GTK4_AVAILABLE or Gtk4 is None:
    debug = (os.environ.get("PKGER_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")
    if debug:
        import traceback
        print("GTK4 load failure details:", file=sys.stderr)
        if _gtk4_exc is not None:
            traceback.print_exception(type(_gtk4_exc), _gtk4_exc, _gtk4_exc.__traceback__)
        else:
            print("(no exception captured during Gtk4 import)", file=sys.stderr)
        print(f"GI_TYPELIB_PATH={os.environ.get('GI_TYPELIB_PATH','')}", file=sys.stderr)
        print(f"LD_LIBRARY_PATH={os.environ.get('LD_LIBRARY_PATH','')}", file=sys.stderr)
        print(f"PATH={os.environ.get('PATH','')}", file=sys.stderr)
        print(f"sys.path={sys.path}", file=sys.stderr)
    
    # Fallback attempt for Nuitka: try to import without gi.require_version if it failed
    if not GTK4_AVAILABLE:
        try:
            from gi.repository import Gtk as Gtk4, Gdk as Gdk4, Gio as Gio4, GObject as GObject4
            GTK4_AVAILABLE = True
            if debug: print("DEBUG: GTK4 loaded via fallback import", file=sys.stderr)
        except Exception as e:
            if debug: print(f"DEBUG: Fallback import failed: {e}", file=sys.stderr)

    if not GTK4_AVAILABLE or Gtk4 is None:
        print("GTK4 is required. Install it with: pacman -S gtk4")
        if not debug:
            print("Tip: Run with PKGER_DEBUG=1 to see detailed error logs.")
        sys.exit(1)

APPINDICATOR_AVAILABLE = False
AppIndicator3 = None
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # type: ignore
    APPINDICATOR_AVAILABLE = True
except Exception:
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3  # type: ignore
        APPINDICATOR_AVAILABLE = True
    except Exception:
        APPINDICATOR_AVAILABLE = False

APP_NAME = "PKGER"
APP_VERSION = "1.0.1"
__version__ = APP_VERSION
APP_DEVELOPER = "almezali"
APP_YEAR = "2026"

log = logging.getLogger(__name__)

ARCH_NEWS_RSS = "https://archlinux.org/feeds/news/"


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

# GTK 3 UI removed: GTK 4-only build

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

    class PKGERGtk4Window(Gtk4.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=f"{APP_NAME} {APP_VERSION}")
            self.set_default_size(1360, 860)
            self.set_resizable(True)
            self._is_fullscreen4 = False

            self.packages_cache = []
            self.current_selection = None
            self.updates_data4 = []
            self.repo_data4 = {}
            self.selected_repo4 = None
            self.selected_repo_pkg4 = None
            self.selected_repo_pkg_names4 = set()
            self.command_history4 = []
            self.search_timer_id4 = 0
            self.appimages_data4 = []
            self.sudo_password4 = None
            self.pkg_client_filter4 = ""
            self.pkg_installed_only4 = False
            self.repo_pkg_filter4 = ""
            self.repo_installed_only4 = False
            self.system_log_buf4 = None
            self.pkg_sort_key4 = "name"
            self.pkg_sort_asc4 = True

            self._apply_css()
            self._build_ui()
            self._load_installed_packages()
            self._load_updates()
            self._load_repos()
            self._load_appimages4()
            self._refresh_dashboard()

        def _apply_css(self):
            css = """
            window { background: #262b33; color: #f3f6fb; }
            headerbar { background: #303743; color: #f3f6fb; border-bottom: 1px solid #4d5870; }
            menubutton.header-menu-toggle { min-width: 40px; min-height: 32px; padding: 4px 10px; }
            menubutton.header-menu-toggle button { border-radius: 8px; }
            /* Header menu uses Gtk.PopoverMenu from Gio.Menu (platform-native rows, no custom scroll box) */
            .card { background: rgba(58, 67, 81, 0.78); border: 1px solid #616d83; border-radius: 10px; padding: 8px; }
            .toolbar { background: rgba(66, 75, 91, 0.78); border: 1px solid #647089; border-radius: 8px; padding: 8px; }
            .section-title { font-weight: 700; font-size: 13px; color: #ffffff; letter-spacing: .2px; }
            entry, dropdown, textview { background: #333c49; color: #f3f6fb; border: 1px solid #5f6c84; border-radius: 8px; }
            button { background: #3c4759; color: #f3f6fb; border: 1px solid #66738d; border-radius: 8px; padding: 7px 12px; }
            button:hover { background: #46536a; }
            button.suggested-action { background: #2b8cff; border-color: #1f76dd; color: #ffffff; }
            listview, row { background: #2d3440; color: #f3f6fb; }
            progressbar trough { background: #394355; border-radius: 999px; }
            progressbar progress { background: #2b8cff; border-radius: 999px; }
            """
            provider = Gtk4.CssProvider()
            provider.load_from_data(css.encode("utf-8"))
            Gtk4.StyleContext.add_provider_for_display(
                Gdk4.Display.get_default(),
                provider,
                Gtk4.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        def _build_ui(self):
            header = Gtk4.HeaderBar()
            title = Gtk4.Label(label=APP_NAME)
            title.add_css_class("section-title")
            header.set_title_widget(title)
            fullscreen_btn = Gtk4.Button()
            fullscreen_btn.set_icon_name("view-fullscreen-symbolic")
            fullscreen_btn.set_tooltip_text("Toggle fullscreen (F11) · Esc to exit")
            fullscreen_btn.connect("clicked", lambda *_: self._toggle_fullscreen4())

            self._gtk4_overflow_menubtn4 = Gtk4.MenuButton()
            self._gtk4_overflow_menubtn4.set_icon_name("view-more-horizontal-symbolic")
            self._gtk4_overflow_menubtn4.add_css_class("header-menu-toggle")
            self._gtk4_overflow_menubtn4.set_tooltip_text(
                "All actions — keyboard Menu or Shift+F10"
            )
            # Outer-right: fullscreen; to its left: overflow menu (always visible)
            header.pack_end(fullscreen_btn)
            header.pack_end(self._gtk4_overflow_menubtn4)
            self.set_titlebar(header)

            key_ctrl = Gtk4.EventControllerKey.new()
            key_ctrl.connect("key-pressed", self._on_key_pressed_main4)
            self.add_controller(key_ctrl)

            root = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            root.set_margin_top(8)
            root.set_margin_bottom(8)
            root.set_margin_start(8)
            root.set_margin_end(8)
            self.set_child(root)

            shell = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            root.append(shell)

            self.left_nav4 = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            self.left_nav4.add_css_class("card")
            self.left_nav4.set_size_request(130, -1)
            self.left_nav_revealer4 = Gtk4.Revealer()
            self.left_nav_revealer4.set_transition_type(Gtk4.RevealerTransitionType.SLIDE_RIGHT)
            self.left_nav_revealer4.set_reveal_child(True)
            self.left_nav_revealer4.set_child(self.left_nav4)
            shell.append(self.left_nav_revealer4)
            self.left_nav4.append(Gtk4.Label(label="◈"))
            for label, page in (
                ("Install", "install"),
                ("Updates", "updates"),
                ("Repos", "repos"),
                ("AppImages", "appimages"),
                ("System", "system"),
            ):
                b = Gtk4.Button(label=label)
                b.connect("clicked", lambda *_a, p=page: self.mode_stack4.set_visible_child_name(p))
                b.set_tooltip_text(f"Open {label} workspace")
                self.left_nav4.append(b)
            self.left_nav4.append(Gtk4.Separator(orientation=Gtk4.Orientation.HORIZONTAL))
            for label, action in (("Cache Cleaner", "clean"),):
                b = Gtk4.Button(label=label)
                b.connect("clicked", lambda *_a, x=action: self._run_system_action(x))
                self.left_nav4.append(b)
            news_btn = Gtk4.Button(label="Arch News")
            news_btn.connect("clicked", lambda *_: self._load_arch_news4())
            self.left_nav4.append(news_btn)
            about_btn = Gtk4.Button(label="About")
            about_btn.connect("clicked", lambda *_: self._show_about4())
            self.left_nav4.append(about_btn)

            content = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            content.set_hexpand(True)
            content.set_vexpand(True)
            shell.append(content)

            tabs_bar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            tabs_bar.add_css_class("toolbar")
            self.left_nav_toggle_btn4 = Gtk4.Button(label="☰")
            self.left_nav_toggle_btn4.connect("clicked", lambda *_: self._toggle_left_nav4())
            tabs_bar.append(self.left_nav_toggle_btn4)
            self.mode_stack4 = Gtk4.Stack()
            self.mode_stack4.set_transition_type(Gtk4.StackTransitionType.SLIDE_LEFT_RIGHT)
            self.section_title4 = Gtk4.Label(label="Install")
            self.section_title4.add_css_class("section-title")
            self.section_title4.set_xalign(0)
            tabs_bar.append(self.section_title4)
            content.append(tabs_bar)

            self.mode_stack4.add_titled(self._create_install_workspace4(), "install", "Install")
            self.mode_stack4.add_titled(self._create_updates_workspace4(), "updates", "Updates")
            self.mode_stack4.add_titled(self._create_repos_page_gtk4(), "repos", "Repos")
            self.mode_stack4.add_titled(self._create_appimages_page4(), "appimages", "AppImages")
            self.mode_stack4.add_titled(self._create_system_workspace(), "system", "System")
            self.mode_stack4.connect("notify::visible-child-name", self._on_mode_changed4)
            content.append(self.mode_stack4)

            bottom_bar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            bottom_bar.add_css_class("toolbar")
            self.history_btn4 = Gtk4.Button(label="History")
            self.history_btn4.connect("clicked", lambda *_: self._show_history4())
            self.history_btn4.set_tooltip_text("Show executed commands history")
            self.updates_badge_btn4 = Gtk4.Button(label="Updates (0)")
            self.updates_badge_btn4.connect("clicked", lambda *_: self.mode_stack4.set_visible_child_name("updates"))
            self.primary_action_btn4 = Gtk4.Button(label="Upgrade All")
            self.primary_action_btn4.add_css_class("suggested-action")
            self.primary_action_btn4.connect("clicked", lambda *_: self._run_system_action("update"))
            bottom_bar.append(self.history_btn4)
            bottom_bar.append(self.updates_badge_btn4)
            bottom_bar.append(self.primary_action_btn4)
            content.append(bottom_bar)

            self.progress = Gtk4.ProgressBar()
            self.progress.set_fraction(0.0)
            root.append(self.progress)

            self.status_label = Gtk4.Label(label="Ready")
            self.status_label.set_xalign(0)
            root.append(self.status_label)

            self._gtk4_finish_header_overflow_popover()

        def _gtk4_register_overflow_win_actions(self):
            """Register window actions for the header overflow Gio.Menu (native PopoverMenu)."""
            if getattr(self, "_pkger_gtk4_overflow_actions_done", False):
                return

            def reg(name, fn):
                act = Gio4.SimpleAction.new(name, None)
                act.connect("activate", lambda _a, _p, f=fn: f())
                self.add_action(act)

            reg("pkgo-go-install", lambda: self.mode_stack4.set_visible_child_name("install"))
            reg("pkgo-refresh-install-list", self._refresh_install_list4)
            reg("pkgo-install-local-pkg", self._install_local_package4)
            reg("pkgo-install-selected", lambda: self._run_pkg_action("install"))
            reg("pkgo-remove-selected", lambda: self._run_pkg_action("remove"))
            reg("pkgo-list-native", lambda: self._gtk4_quick_install_mode("installed_native"))
            reg("pkgo-list-foreign", lambda: self._gtk4_quick_install_mode("installed_foreign"))
            reg("pkgo-list-flatpak", lambda: self._gtk4_quick_install_mode("installed_flatpak"))
            reg("pkgo-list-appimage", lambda: self._gtk4_quick_install_mode("installed_appimage"))
            reg("pkgo-go-system", lambda: self.mode_stack4.set_visible_child_name("system"))
            reg("pkgo-sys-upgrade", lambda: self._run_system_action("update"))
            reg("pkgo-sys-aur-upgrade", self._gtk4_aur_full_upgrade)
            reg("pkgo-sys-sync", lambda: self._run_system_action("sync"))
            reg("pkgo-sys-clean-cache", lambda: self._run_system_action("clean"))
            reg("pkgo-sys-deep-clean", lambda: self._gtk4_maybe_run_stream("deepclean"))
            reg("pkgo-sys-paccache", lambda: self._gtk4_maybe_run_stream("paccache"))
            reg("pkgo-sys-orphans", lambda: self._gtk4_maybe_run_stream("orphans"))
            reg("pkgo-sys-filedb", lambda: self._gtk4_maybe_run_stream("fy"))
            reg("pkgo-sys-keyring", lambda: self._gtk4_maybe_run_stream("keyring"))
            reg("pkgo-app-refresh-all", self._gtk4_refresh_all_data4)
            reg("pkgo-app-fullscreen", self._toggle_fullscreen4)
            reg("pkgo-app-arch-news", self._load_arch_news4)
            reg("pkgo-app-pacman-log", self._gtk4_reload_pacman_log)
            reg("pkgo-app-about", self._show_about4)
            reg("pkgo-app-quit", lambda: self.close())
            self._pkger_gtk4_overflow_actions_done = True

        def _gtk4_finish_header_overflow_popover(self):
            """Attach a standard Gio.Menu so Gtk shows a native menu (no single-line scroll box)."""
            self._gtk4_register_overflow_win_actions()
            btn = self._gtk4_overflow_menubtn4

            pkg = Gio4.Menu()
            pkg.append("Go to Install workspace", "win.pkgo-go-install")
            pkg.append(
                "Reload package list (current search & install mode)",
                "win.pkgo-refresh-install-list",
            )
            pkg.append(
                "Install package from local .pkg.tar.* file (pacman -U)",
                "win.pkgo-install-local-pkg",
            )
            pkg.append(
                "Install selected row (pacman / AUR helper / flatpak install)",
                "win.pkgo-install-selected",
            )
            pkg.append(
                "Remove selected installed package (pacman -R / flatpak uninstall)",
                "win.pkgo-remove-selected",
            )
            pkg.append(
                "Show packages from official repos only (pacman -Qen)",
                "win.pkgo-list-native",
            )
            pkg.append(
                "Show foreign / AUR packages (pacman -Qm)",
                "win.pkgo-list-foreign",
            )
            pkg.append(
                "Show installed Flatpak apps (flatpak list)",
                "win.pkgo-list-flatpak",
            )
            pkg.append(
                "Show AppImages tracked by PKGER",
                "win.pkgo-list-appimage",
            )

            sysm = Gio4.Menu()
            sysm.append(
                "Go to System & maintenance workspace",
                "win.pkgo-go-system",
            )
            sysm.append(
                "Upgrade all official packages (pacman -Syu)",
                "win.pkgo-sys-upgrade",
            )
            sysm.append(
                "Upgrade system and AUR (yay or paru -Syu)",
                "win.pkgo-sys-aur-upgrade",
            )
            sysm.append(
                "Synchronize databases only (pacman -Sy)",
                "win.pkgo-sys-sync",
            )
            sysm.append(
                "Trim package cache, keep recent versions (pacman -Sc)",
                "win.pkgo-sys-clean-cache",
            )
            sysm.append(
                "Empty entire package cache (pacman -Scc)",
                "win.pkgo-sys-deep-clean",
            )
            sysm.append(
                "Run paccache: keep last 2 versions, remove older",
                "win.pkgo-sys-paccache",
            )
            sysm.append(
                "Remove orphan packages (no longer required by others)",
                "win.pkgo-sys-orphans",
            )
            sysm.append(
                "Download file database for pacman -F / pkgfile (pacman -Fy)",
                "win.pkgo-sys-filedb",
            )
            sysm.append(
                "Refresh pacman keyring (pacman-key --refresh-keys)",
                "win.pkgo-sys-keyring",
            )

            appm = Gio4.Menu()
            appm.append(
                "Reload all data: install list, updates, repos, AppImages",
                "win.pkgo-app-refresh-all",
            )
            appm.append(
                "Toggle fullscreen window (same as F11)",
                "win.pkgo-app-fullscreen",
            )
            appm.append(
                "Load Arch Linux news from RSS into output",
                "win.pkgo-app-arch-news",
            )
            appm.append(
                "Show recent pacman log lines in output pane",
                "win.pkgo-app-pacman-log",
            )
            appm.append(
                "Show PKGER version and credits in output",
                "win.pkgo-app-about",
            )
            appm.append("Quit PKGER", "win.pkgo-app-quit")

            menu = Gio4.Menu()
            menu.append_section("Packages", pkg)
            menu.append_section("System & maintenance", sysm)
            menu.append_section("Application", appm)
            btn.set_menu_model(menu)

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

        def _create_index_workspace4(self):
            panel = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("card")
            title = Gtk4.Label(label="Global Index")
            title.set_xalign(0)
            title.add_css_class("section-title")
            panel.append(title)
            search = Gtk4.SearchEntry()
            search.set_placeholder_text("Filter indexed actions...")
            panel.append(search)
            listbox = Gtk4.ListBox()
            listbox.set_selection_mode(Gtk4.SelectionMode.NONE)
            panel.append(listbox)

            items = [
                ("Install workspace", "install"),
                ("Updates workspace", "updates"),
                ("Repositories workspace", "repos"),
                ("AppImages workspace", "appimages"),
                ("System workspace", "system"),
                ("Run full upgrade", "act_upgrade"),
                ("Load updates", "act_load_updates"),
                ("Load repositories", "act_load_repos"),
                ("Load AppImages", "act_load_appimages"),
            ]

            def rebuild():
                self._clear_listbox(listbox)
                q = search.get_text().strip().lower()
                for label, action in items:
                    if q and q not in label.lower():
                        continue
                    row = Gtk4.ListBoxRow()
                    b = Gtk4.Button(label=label)
                    b.set_halign(Gtk4.Align.FILL)
                    b.connect("clicked", lambda _btn, a=action: self._run_index_action4(a))
                    row.set_child(b)
                    listbox.append(row)

            search.connect("search-changed", lambda *_: rebuild())
            rebuild()
            return panel

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

        def _create_install_workspace4(self):
            panel = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("card")

            controls = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.search_entry4 = Gtk4.SearchEntry()
            self.search_entry4.set_placeholder_text(
                "Name, description, or developer (Developer mode); filter client-side below…"
            )
            self.search_entry4.connect("activate", lambda *_: self._search_packages())
            self.search_entry4.connect("search-changed", lambda *_: self._schedule_search4())
            self.search_type4 = Gtk4.DropDown.new_from_strings(list(INSTALL_COMBO_TO_KEY.keys()))
            self.search_type4.connect("notify::selected-item", self._on_install_mode_changed4)
            refresh_btn = Gtk4.Button(label="↻")
            refresh_btn.set_tooltip_text("Reload list for the current source (or all pacman -Q when query is empty on Unified)")
            refresh_btn.connect("clicked", lambda *_: self._refresh_install_list4())
            install_local_btn = Gtk4.Button(label="Install Local")
            install_local_btn.connect("clicked", lambda *_: self._install_local_package4())
            install_local_btn.set_tooltip_text("Install local .pkg.tar.* package file")
            controls.append(self.search_entry4)
            controls.append(refresh_btn)
            controls.append(self.search_type4)
            controls.append(install_local_btn)
            panel.append(controls)

            index_controls = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.pkg_filter_entry4 = Gtk4.SearchEntry()
            self.pkg_filter_entry4.set_placeholder_text("Index filter (client-side) ...")
            self.pkg_filter_entry4.connect("search-changed", self._on_pkg_client_filter_changed4)
            self.pkg_installed_check4 = Gtk4.CheckButton(label="Installed only")
            self.pkg_installed_check4.connect("toggled", self._on_pkg_client_filter_changed4)
            export_visible_btn = Gtk4.Button(label="Export Visible")
            export_visible_btn.connect("clicked", lambda *_: self._export_visible_packages4())
            index_controls.append(self.pkg_filter_entry4)
            index_controls.append(self.pkg_installed_check4)
            index_controls.append(export_visible_btn)
            panel.append(index_controls)

            sort_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=4)
            sort_row.add_css_class("toolbar")
            sort_row.append(Gtk4.Label(label="Sort (tap again to reverse):"))
            for label, colkey in (
                ("Name", "name"),
                ("Version", "version"),
                ("Repo", "repository"),
                ("Installed", "installed"),
                ("Description", "description"),
            ):
                sb = Gtk4.Button(label=label)
                sb.set_tooltip_text(f"Sort by {label}")
                sb.connect("clicked", lambda *_a, k=colkey: self._apply_pkg_sort_toggle4(k))
                sort_row.append(sb)
            panel.append(sort_row)

            body = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)
            panel.append(body)

            left = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            self.pkg_store4 = Gio4.ListStore.new(PackageRow4)
            self.pkg_selection4 = Gtk4.SingleSelection(model=self.pkg_store4)
            self.pkg_selection4.set_can_unselect(True)
            self.pkg_selection4.connect("notify::selected-item", self._on_pkg_table_selected4)
            self.pkg_table4 = Gtk4.ColumnView(model=self.pkg_selection4)
            self.pkg_table4.set_vexpand(True)
            self.pkg_table4.append_column(self._create_text_column4("Name", "name", expand=True))
            self.pkg_table4.append_column(self._create_text_column4("Version", "version"))
            self.pkg_table4.append_column(self._create_text_column4("Repository", "repository"))
            self.pkg_table4.append_column(self._create_text_column4("Installed", "installed"))
            self.pkg_table4.append_column(self._create_text_column4("Description", "description", expand=True))
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.pkg_table4)
            left.append(sw)
            body.set_start_child(left)

            right = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=6)
            action_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.install_btn4 = Gtk4.Button(label="Install")
            self.install_btn4.connect("clicked", lambda *_: self._run_pkg_action("install"))
            self.remove_btn4 = Gtk4.Button(label="Remove")
            self.remove_btn4.connect("clicked", lambda *_: self._run_pkg_action("remove"))
            self.update_btn4 = Gtk4.Button(label="Perform Upgrade")
            self.update_btn4.add_css_class("suggested-action")
            self.update_btn4.connect("clicked", lambda *_: self._run_system_action("update"))
            action_row.append(self.install_btn4)
            action_row.append(self.remove_btn4)
            action_row.append(self.update_btn4)
            self.install_btn4.set_sensitive(False)
            self.remove_btn4.set_sensitive(False)
            self.install_btn4.set_tooltip_text("Install selected package")
            self.remove_btn4.set_tooltip_text("Remove selected installed package")
            self.update_btn4.set_tooltip_text("Run full system upgrade")
            right.append(action_row)
            self.details_view4 = Gtk4.TextView()
            self.details_view4.set_editable(False)
            self.details_view4.set_monospace(True)
            self.details_buf4 = self.details_view4.get_buffer()
            self.details_buf4.set_text("Select package for details.")
            dsw = Gtk4.ScrolledWindow()
            dsw.set_vexpand(True)
            dsw.set_child(self.details_view4)
            right.append(dsw)
            self.output_view4 = Gtk4.TextView()
            self.output_view4.set_editable(False)
            self.output_view4.set_monospace(True)
            self.output_buf4 = self.output_view4.get_buffer()
            lsw = Gtk4.ScrolledWindow()
            lsw.set_vexpand(True)
            lsw.set_child(self.output_view4)
            right.append(lsw)
            body.set_end_child(right)
            return panel

        def _create_updates_workspace4(self):
            panel = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            panel.add_css_class("card")
            toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            refresh = Gtk4.Button(label="Refresh")
            refresh.connect("clicked", lambda *_: self._load_updates())
            apply_btn = Gtk4.Button(label="Update Selected")
            apply_btn.add_css_class("suggested-action")
            apply_btn.connect("clicked", lambda *_: self._apply_selected_updates())
            apply_btn.set_tooltip_text("Apply only checked updates")
            toolbar.append(refresh)
            toolbar.append(apply_btn)
            panel.append(toolbar)
            body = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)
            panel.append(body)
            self.updates_list4 = Gtk4.ListBox()
            self.updates_list4.set_selection_mode(Gtk4.SelectionMode.SINGLE)
            self.updates_list4.connect("row-selected", self._on_update_selected4)
            swl = Gtk4.ScrolledWindow()
            swl.set_vexpand(True)
            swl.set_child(self.updates_list4)
            body.set_start_child(swl)
            self.update_preview4 = Gtk4.TextView()
            self.update_preview4.set_editable(False)
            self.update_preview4.set_monospace(True)
            self.update_preview_buf4 = self.update_preview4.get_buffer()
            self.update_preview_buf4.set_text("Select an update for details.")
            swr = Gtk4.ScrolledWindow()
            swr.set_vexpand(True)
            swr.set_child(self.update_preview4)
            body.set_end_child(swr)
            return panel

        def _create_appimages_page4(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            scan_btn = Gtk4.Button(label="Scan")
            scan_btn.connect("clicked", lambda *_: self._load_appimages4())
            import_btn = Gtk4.Button(label="Import AppImage")
            import_btn.connect("clicked", lambda *_: self._import_appimage4())
            launch_btn = Gtk4.Button(label="Launch Selected")
            launch_btn.connect("clicked", lambda *_: self._launch_appimage4())
            remove_btn = Gtk4.Button(label="Remove Selected")
            remove_btn.connect("clicked", lambda *_: self._remove_appimage4())
            for b in (scan_btn, import_btn, launch_btn, remove_btn):
                toolbar.append(b)
            box.append(toolbar)

            self.appimage_list4 = Gtk4.ListBox()
            self.appimage_list4.connect("row-selected", self._on_appimage_selected4)
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.appimage_list4)
            box.append(sw)

            self.appimage_details4 = Gtk4.TextView()
            self.appimage_details4.set_editable(False)
            self.appimage_details4.set_monospace(True)
            self.appimage_details_buf4 = self.appimage_details4.get_buffer()
            self.appimage_details_buf4.set_text("Select an AppImage entry.")
            dsw = Gtk4.ScrolledWindow()
            dsw.set_vexpand(True)
            dsw.set_child(self.appimage_details4)
            box.append(dsw)
            return box

        def _create_dashboard_workspace(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=10)
            box.add_css_class("card")

            title = Gtk4.Label(label="PKGER Command Center")
            title.set_xalign(0)
            title.add_css_class("section-title")
            box.append(title)

            self.dash_summary4 = Gtk4.Label(label="Loading statistics...")
            self.dash_summary4.set_xalign(0)
            self.dash_summary4.set_wrap(True)
            box.append(self.dash_summary4)

            quick = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            for label, cb in (
                ("Refresh Stats", lambda *_: self._refresh_dashboard()),
                ("Load Updates", lambda *_: self._load_updates()),
                ("Load Repositories", lambda *_: self._load_repos()),
            ):
                b = Gtk4.Button(label=label)
                b.connect("clicked", cb)
                quick.append(b)
            box.append(quick)

            go_row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            for label, page in (
                ("Go Packages", "packages"),
                ("Go Management", "management"),
                ("Go System", "system"),
            ):
                b = Gtk4.Button(label=label)
                b.connect("clicked", lambda *_args, p=page: self.top_stack.set_visible_child_name(p))
                go_row.append(b)
            box.append(go_row)
            return box

        def _create_packages_workspace(self):
            pane = Gtk4.Paned.new(Gtk4.Orientation.HORIZONTAL)

            left = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            left.add_css_class("card")
            pane.set_start_child(left)
            pane.set_resize_start_child(True)
            pane.set_shrink_start_child(True)

            title = Gtk4.Label(label="Package List")
            title.set_xalign(0)
            title.add_css_class("section-title")
            left.append(title)

            pw_hint = Gtk4.Label(
                label="Search, developer/packager mode, sorting, and per-source installed lists are on the Install tab (◈ Install) so this view stays in sync."
            )
            pw_hint.set_wrap(True)
            pw_hint.set_xalign(0)
            left.append(pw_hint)

            self.pkg_list = Gtk4.ListBox()
            self.pkg_list.set_selection_mode(Gtk4.SelectionMode.SINGLE)
            self.pkg_list.connect("row-selected", self._on_pkg_selected)
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.pkg_list)
            left.append(sw)

            right_shell = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            right_shell.add_css_class("card")
            pane.set_end_child(right_shell)

            self.pkg_stack = Gtk4.Stack()
            self.pkg_stack.set_transition_type(Gtk4.StackTransitionType.SLIDE_UP_DOWN)
            sidebar = Gtk4.StackSidebar()
            sidebar.set_stack(self.pkg_stack)
            right_shell.append(sidebar)
            right_shell.append(self.pkg_stack)

            self.pkg_stack.add_titled(self._create_operations_page_gtk4(), "ops", "Operations")
            self.pkg_stack.add_titled(self._create_details_page_gtk4(), "details", "Details")
            return pane

        def _create_operations_page_gtk4(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            box.set_hexpand(True)
            box.set_vexpand(True)

            hint = Gtk4.Label(label="Interactive operations terminal: run diagnostics and package queries directly.")
            hint.set_xalign(0)
            hint.set_wrap(True)
            box.append(hint)

            row = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.install_btn4 = Gtk4.Button(label="Install")
            self.install_btn4.connect("clicked", lambda *_: self._run_pkg_action("install"))
            self.remove_btn4 = Gtk4.Button(label="Remove")
            self.remove_btn4.connect("clicked", lambda *_: self._run_pkg_action("remove"))
            self.reinstall_btn4 = Gtk4.Button(label="Reinstall")
            self.reinstall_btn4.connect("clicked", lambda *_: self._run_pkg_action("reinstall"))
            self.update_btn4 = Gtk4.Button(label="Update System")
            self.update_btn4.add_css_class("suggested-action")
            self.update_btn4.connect("clicked", lambda *_: self._run_system_action("update"))
            for b in (self.install_btn4, self.remove_btn4, self.reinstall_btn4, self.update_btn4):
                row.append(b)
            box.append(row)

            self.output_view4 = Gtk4.TextView()
            self.output_view4.set_editable(False)
            self.output_view4.set_monospace(True)
            self.output_buf4 = self.output_view4.get_buffer()
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.output_view4)
            box.append(sw)

            terminal_toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            self.ops_cmd_entry4 = Gtk4.Entry()
            self.ops_cmd_entry4.set_placeholder_text("Type command (example: pacman -Qi bash)")
            self.ops_cmd_entry4.connect("activate", self._ops_run_interactive_command4)
            run_btn = Gtk4.Button(label="Run")
            run_btn.add_css_class("suggested-action")
            run_btn.connect("clicked", self._ops_run_interactive_command4)
            clear_btn = Gtk4.Button(label="Clear")
            clear_btn.connect("clicked", lambda *_: self.output_buf4.set_text(""))
            terminal_toolbar.append(self.ops_cmd_entry4)
            terminal_toolbar.append(run_btn)
            terminal_toolbar.append(clear_btn)
            box.append(terminal_toolbar)

            quick = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            for label, cmd in (
                ("Installed", "pacman -Qe"),
                ("Orphans", "pacman -Qdt"),
                ("Updates", "pacman -Qu"),
                ("Disk", "df -h"),
            ):
                b = Gtk4.Button(label=label)
                b.connect("clicked", lambda _btn, c=cmd: self._ops_run_interactive_command4(command_text=c))
                quick.append(b)
            box.append(quick)

            self._append_output("GTK4 Operations terminal ready.")
            self._append_output("Tip: run non-privileged diagnostics here, or select a package for actions.")
            return box

        def _create_details_page_gtk4(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)
            box.set_hexpand(True)
            box.set_vexpand(True)
            self.details_view4 = Gtk4.TextView()
            self.details_view4.set_editable(False)
            self.details_view4.set_monospace(True)
            self.details_buf4 = self.details_view4.get_buffer()
            self.details_buf4.set_text("Select a package to view details.")
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.details_view4)
            box.append(sw)
            return box

        def _create_management_workspace(self):
            shell = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=8)
            shell.add_css_class("card")

            self.mgmt_stack = Gtk4.Stack()
            self.mgmt_stack.set_transition_type(Gtk4.StackTransitionType.SLIDE_UP_DOWN)
            sidebar = Gtk4.StackSidebar()
            sidebar.set_stack(self.mgmt_stack)
            shell.append(sidebar)
            shell.append(self.mgmt_stack)

            self.mgmt_stack.add_titled(self._create_updates_page_gtk4(), "updates", "Updates")
            self.mgmt_stack.add_titled(self._create_repos_page_gtk4(), "repos", "Repositories")
            return shell

        def _create_updates_page_gtk4(self):
            box = Gtk4.Box(orientation=Gtk4.Orientation.VERTICAL, spacing=8)

            toolbar = Gtk4.Box(orientation=Gtk4.Orientation.HORIZONTAL, spacing=6)
            refresh = Gtk4.Button(label="Refresh Updates")
            refresh.connect("clicked", lambda *_: self._load_updates())
            apply_btn = Gtk4.Button(label="Apply Selected")
            apply_btn.add_css_class("suggested-action")
            apply_btn.connect("clicked", lambda *_: self._apply_selected_updates())
            toolbar.append(refresh)
            toolbar.append(apply_btn)
            box.append(toolbar)

            self.updates_list4 = Gtk4.ListBox()
            self.updates_list4.set_selection_mode(Gtk4.SelectionMode.NONE)
            sw = Gtk4.ScrolledWindow()
            sw.set_vexpand(True)
            sw.set_child(self.updates_list4)
            box.append(sw)
            return box

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

        def _gtk4_diag_cmd(self, argv, title, timeout=120):
            def worker():
                try:
                    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
                    out = (r.stdout or "") + (r.stderr or "")
                    GLib.idle_add(self._gtk4_append_block, title, out)
                except Exception as exc:
                    GLib.idle_add(self._append_output, f"{title} failed: {exc}")

            threading.Thread(target=worker, daemon=True).start()

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

        def _gtk4_file_owns(self):
            path = self.sys_file_owns4.get_text().strip() if hasattr(self, "sys_file_owns4") else ""
            if not path:
                return
            self._gtk4_diag_cmd(["pacman", "-Qo", path], f"pacman -Qo {path}", 30)

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

        def _append_output(self, text):
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {text}\n"
            end = self.output_buf4.get_end_iter()
            self.output_buf4.insert(end, line)
            buf = getattr(self, "system_log_buf4", None)
            if buf is not None:
                e2 = buf.get_end_iter()
                buf.insert(e2, line)

        def _set_status(self, text):
            self.status_label.set_text(text)

        def _set_progress(self, value):
            self.progress.set_fraction(max(0.0, min(1.0, value / 100.0)))

        def _on_install_mode_changed4(self, *_args):
            idx = self.search_type4.get_selected()
            if idx is None or idx < 0:
                return
            if idx >= len(GTK4_INSTALL_MODE_KEYS):
                return
            mode = GTK4_INSTALL_MODE_KEYS[idx]
            if mode in INSTALL_SUBSET_KEYS:
                self._search_packages()

        def _refresh_install_list4(self):
            query = self.search_entry4.get_text().strip()
            idx = self.search_type4.get_selected()
            mode = GTK4_INSTALL_MODE_KEYS[idx if idx is not None else 0]
            if not query and mode == "unified":
                self._load_installed_packages()
                return
            self._search_packages()

        def _apply_pkg_sort_toggle4(self, column_key):
            if self.pkg_sort_key4 == column_key:
                self.pkg_sort_asc4 = not self.pkg_sort_asc4
            else:
                self.pkg_sort_key4 = column_key
                self.pkg_sort_asc4 = True
            self._sort_packages_cache4()
            self._refresh_pkg_list_view4()

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

            def worker():
                try:
                    pkgs = unified_search_packages(query, mode)
                except Exception:
                    pkgs = []
                GLib.idle_add(self._populate_pkg_list, pkgs)
                GLib.idle_add(self._set_status, f"Found {len(pkgs)} packages")

            threading.Thread(target=worker, daemon=True).start()

        def _schedule_search4(self):
            if self.search_timer_id4:
                try:
                    GLib.source_remove(self.search_timer_id4)
                except Exception:
                    pass
                self.search_timer_id4 = 0
            self.search_timer_id4 = GLib.timeout_add(220, self._run_scheduled_search4)
            return False

        def _run_scheduled_search4(self):
            self.search_timer_id4 = 0
            self._search_packages()
            return False

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

        def _refresh_pkg_list_view4(self):
            if not hasattr(self, "pkg_store4"):
                return
            self.pkg_store4.remove_all()
            q = (self.pkg_filter_entry4.get_text().strip().lower() if hasattr(self, "pkg_filter_entry4") else "")
            installed_only = self.pkg_installed_check4.get_active() if hasattr(self, "pkg_installed_check4") else False
            for item in self.packages_cache:
                repo_name = item.get("repo") or item.get("repository") or "unknown"
                pkg_name = item.get("name") or item.get("pkg") or "-"
                hay = f"{pkg_name} {repo_name} {item.get('description','')}".lower()
                if q and q not in hay:
                    continue
                if installed_only and not item.get("installed", False):
                    continue
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

        def _on_pkg_client_filter_changed4(self, *_):
            self._refresh_pkg_list_view4()

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
                        txt = r.stdout
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

        def _run_system_action(self, action):
            """Shortcuts from nav / Install tab — same confirmations as System workspace."""
            route = {
                "update": "update",
                "clean": "clean",
                "sync": "sync",
                "fixdeps": "fixdeps",
            }
            if action in route:
                self._gtk4_maybe_run_stream(route[action])
            else:
                self._gtk4_maybe_run_stream("clean")

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

        def _load_updates(self):
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

        def _on_update_selected4(self, _list, row):
            if row is None:
                self.update_preview_buf4.set_text("Select an update for details.")
                return
            item = row.update_item
            self.update_preview_buf4.set_text(
                f"Package: {item['name']}\nCurrent: {item['from']}\nNew: {item['to']}\n\n"
                "Use Update Selected or Upgrade All to apply."
            )

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

        def _on_appimage_selected4(self, _list, row):
            if row is None:
                self.appimage_details_buf4.set_text("Select an AppImage entry.")
                self.selected_appimage4 = None
                return
            self.selected_appimage4 = row.appimage_item
            p = self.selected_appimage4.get("description", "").replace("Local AppImage: ", "")
            self.appimage_details_buf4.set_text(
                f"Name: {self.selected_appimage4.get('name','-')}\n"
                f"Type: AppImage\n"
                f"Path: {p}\n"
            )

        def _import_appimage4(self):
            chooser = Gtk4.FileChooserNative.new(
                "Import AppImage",
                self,
                Gtk4.FileChooserAction.OPEN,
                "Import",
                "Cancel",
            )
            filt = Gtk4.FileFilter()
            filt.set_name("AppImage")
            filt.add_pattern("*.AppImage")
            chooser.add_filter(filt)

            def on_response(dialog, response):
                if response != Gtk4.ResponseType.ACCEPT:
                    return
                file_obj = dialog.get_file()
                if file_obj is None:
                    return
                src = file_obj.get_path()
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

            chooser.connect("response", on_response)
            chooser.show()

        def _launch_appimage4(self):
            item = getattr(self, "selected_appimage4", None)
            if not item:
                self._append_output("Select an AppImage first.")
                return
            p = item.get("description", "").replace("Local AppImage: ", "")
            if not p:
                self._append_output("Invalid AppImage path.")
                return
            self._append_output(f"Launching AppImage: {p}")
            subprocess.Popen(["bash", "-lc", shlex.quote(p)])

        def _remove_appimage4(self):
            item = getattr(self, "selected_appimage4", None)
            if not item:
                self._append_output("Select an AppImage first.")
                return
            p = item.get("description", "").replace("Local AppImage: ", "")
            if not p:
                self._append_output("Invalid AppImage path.")
                return
            try:
                os.remove(p)
                self._append_output(f"Removed AppImage: {p}")
                self.selected_appimage4 = None
                self._load_appimages4()
            except Exception as e:
                self._append_output(f"Remove AppImage failed: {e}")

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
            self.selected_repo_pkg_names4.clear()
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
            self.repo_pkg_details_buf4.set_text(
                f"Repository: {repo}\nPackages shown: {shown}\nChecked: {len(self.selected_repo_pkg_names4)}\n\nSelect a package for full details."
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

        def _repo_install_selected4(self):
            if not self.selected_repo_pkg4:
                self._append_output("Select a repository package first.")
                return
            self._run_stream_command(
                ["bash", "-lc", f"sudo pacman -S --noconfirm {shlex.quote(self.selected_repo_pkg4)}"],
                f"install {self.selected_repo_pkg4}",
                requires_privilege=True,
            )

        def _repo_batch_install4(self):
            if not self.selected_repo_pkg_names4:
                self._append_output("No checked repository packages.")
                return
            names = sorted(self.selected_repo_pkg_names4)
            self._run_stream_command(
                ["bash", "-lc", f"pacman -S --noconfirm {' '.join(shlex.quote(n) for n in names)}"],
                f"batch install {len(names)} packages",
                requires_privilege=True,
            )

        def _repo_remove_selected4(self):
            if not self.selected_repo_pkg4:
                self._append_output("Select an installed package first.")
                return
            self._run_stream_command(
                ["bash", "-lc", f"sudo pacman -Rns --noconfirm {shlex.quote(self.selected_repo_pkg4)}"],
                f"remove {self.selected_repo_pkg4}",
                requires_privilege=True,
            )

        def _repo_batch_remove4(self):
            if not self.selected_repo_pkg_names4:
                self._append_output("No checked packages to remove.")
                return
            names = sorted(self.selected_repo_pkg_names4)
            self._run_stream_command(
                ["bash", "-lc", f"pacman -Rns --noconfirm {' '.join(shlex.quote(n) for n in names)}"],
                f"batch remove {len(names)} packages",
                requires_privilege=True,
            )

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

        def _clear_listbox(self, listbox):
            child = listbox.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                listbox.remove(child)
                child = nxt

        def _toggle_left_nav4(self):
            visible = self.left_nav_revealer4.get_reveal_child()
            self.left_nav_revealer4.set_reveal_child(not visible)
            self.left_nav_toggle_btn4.set_label("☰" if visible else "✕")

        def _toggle_fullscreen4(self):
            if self._is_fullscreen4:
                self.unfullscreen()
                self._is_fullscreen4 = False
            else:
                self.fullscreen()
                self._is_fullscreen4 = True

        def _on_key_pressed_main4(self, _controller, keyval, _keycode, state):
            """Menu / Shift+F10: header overflow. F11: fullscreen. Esc: exit fullscreen."""
            menu_key = keyval == Gdk4.KEY_Menu or (
                keyval == Gdk4.KEY_F10 and (state & Gdk4.ModifierType.SHIFT_MASK)
            )
            if menu_key:
                mb = getattr(self, "_gtk4_overflow_menubtn4", None)
                if mb is not None:
                    mb.popup()
                return True
            if keyval == Gdk4.KEY_F11:
                self._toggle_fullscreen4()
                return True
            if keyval == Gdk4.KEY_Escape and self._is_fullscreen4:
                self.unfullscreen()
                self._is_fullscreen4 = False
                return True
            return False

        def _on_mode_changed4(self, *_):
            name = self.mode_stack4.get_visible_child_name() or "install"
            titles = {
                "install": "Install",
                "updates": "Updates",
                "repos": "Repos",
                "appimages": "AppImages",
                "system": "System",
            }
            if hasattr(self, "section_title4"):
                self.section_title4.set_text(titles.get(name, "PKGER"))

        def _show_history4(self):
            if not self.command_history4:
                self._append_output("No history yet.")
                return
            self._append_output("[history] Recent commands:")
            for item in self.command_history4[-20:]:
                self._append_output(f"[{item['time']}] {item['label']} :: {item['cmd']}")

        def _load_arch_news4(self):
            self._set_status("Loading Arch news...")
            def worker():
                items = fetch_arch_news_items(limit=6)
                if not items:
                    GLib.idle_add(self._append_output, "Arch News: No items available.")
                for i, it in enumerate(items, 1):
                    GLib.idle_add(self._append_output, f"[news {i}] {it.get('title','-')}")
                    if it.get("link"):
                        GLib.idle_add(self._append_output, f"  {it['link']}")
                GLib.idle_add(self._set_status, "Arch news loaded")
            threading.Thread(target=worker, daemon=True).start()

        def _show_about4(self):
            about = Gtk4.AboutDialog()
            about.set_transient_for(self)
            about.set_modal(True)
            about.set_program_name(APP_NAME)
            about.set_title(f"About {APP_NAME}")
            about.set_version(f"v {APP_VERSION}")

            about.set_comments(
                "PKGER is a graphical package manager for Arch Linux.\n"
                "Manage pacman packages, updates, repositories, Flatpak, and AppImages."
            )
            if hasattr(about, "set_authors"):
                about.set_authors([APP_DEVELOPER])
            elif hasattr(about, "set_developers"):
                about.set_developers([APP_DEVELOPER])
            about.set_copyright(f"© {APP_YEAR} {APP_DEVELOPER}")

            about.present()
            self._set_status("Showing About")

        def _get_textbuf_text4(self, buf):
            start = buf.get_start_iter()
            end = buf.get_end_iter()
            return buf.get_text(start, end, True) or ""

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
                self._append_output(f"Selected local package: {path}")
                self._run_stream_command(
                    ["bash", "-lc", f"pacman -U --noconfirm {shlex.quote(path)}"],
                    "install local package",
                    requires_privilege=True,
                )

            chooser.connect("response", on_response)
            chooser.show()

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

    class PKGERGtk4Application(Gtk4.Application):
        def __init__(self):
            super().__init__(application_id="com.almezali.pkger.gtk4")

        def do_activate(self):
            win = self.props.active_window
            if win is None:
                win = PKGERGtk4Window(self)
            win.present()


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

    app = PKGERGtk4Application()
    app.run(None)


if __name__ == '__main__':
    main()
