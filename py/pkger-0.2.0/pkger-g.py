#!/usr/bin/env python3
"""
PKGER - Professional Package Manager
Version: v0.2.0-1
Developer: almezali
Date: 2025

GTK edition. Professional, modern UI with package icons.
"""

import os
import sys
import subprocess
import threading
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GObject, GLib, Gdk, GdkPixbuf
except Exception as e:
    print("PyGObject (GTK) is required. Install it with: pacman -S python-gobject gtk3")
    sys.exit(1)

APP_NAME = "PKGER"
APP_VERSION = "v0.2.0-1"
APP_DEVELOPER = "almezali"
APP_YEAR = "2025"

# ----------------------------
# Package Icon Management
# ----------------------------

class PackageIconManager:
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
            "/usr/share/flatpak/exports/share/applications"
        ]
        self.default_icons = {
            'system': 'system-run-symbolic',
            'development': 'applications-development-symbolic',
            'multimedia': 'applications-multimedia-symbolic',
            'office': 'applications-office-symbolic',
            'games': 'applications-games-symbolic',
            'network': 'applications-internet-symbolic',
            'graphics': 'applications-graphics-symbolic',
            'education': 'applications-education-symbolic',
            'science': 'applications-science-symbolic',
            'utility': 'applications-utilities-symbolic'
        }
        self.category_mapping = self._load_category_mapping()
        self.fallback_icons = self._create_fallback_icons()
        
    def _create_fallback_icons(self):
        """Create fallback icons for different package types"""
        icons = {}
        try:
            # Try to create simple colored icons for different categories
            for category, icon_name in self.default_icons.items():
                try:
                    icon = self._load_icon_by_name(icon_name, 24)
                    if icon:
                        icons[category] = icon
                except:
                    pass
        except:
            pass
        return icons
        
    def _load_category_mapping(self):
        """Load package category mapping for better icon selection"""
        mapping = {}
        try:
            # Try to load from Arch package database
            result = subprocess.run(["pacman", "-Si"], capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                current_pkg = None
                for line in result.stdout.split('\n'):
                    if line.startswith('Name'):
                        current_pkg = line.split(':', 1)[1].strip()
                    elif line.startswith('Groups') and current_pkg:
                        groups = line.split(':', 1)[1].strip()
                        if groups:
                            mapping[current_pkg] = groups.split()
        except:
            pass
        
        # Add common package mappings
        common_mappings = {
            'firefox': ['network'],
            'chromium': ['network'],
            'vlc': ['multimedia'],
            'gimp': ['graphics'],
            'libreoffice': ['office'],
            'steam': ['games'],
            'wine': ['utility'],
            'docker': ['development'],
            'git': ['development'],
            'python': ['development'],
            'nodejs': ['development'],
            'gcc': ['development'],
            'clang': ['development'],
            'cmake': ['development'],
            'make': ['development'],
            'vim': ['utility'],
            'emacs': ['utility'],
            'nano': ['utility'],
            'htop': ['utility'],
            'neofetch': ['utility'],
            'btop': ['utility'],
            'glances': ['utility'],
            'systemd': ['system'],
            'linux': ['system'],
            'grub': ['system'],
            'firmware': ['system'],
            'kernel': ['system'],
            'openssh': ['network'],
            'openssl': ['development'],
            'curl': ['network'],
            'wget': ['network'],
            'ffmpeg': ['multimedia'],
            'gstreamer': ['multimedia'],
            'pulseaudio': ['multimedia'],
            'alsa': ['multimedia'],
            'inkscape': ['graphics'],
            'blender': ['graphics'],
            'krita': ['graphics'],
            'darktable': ['graphics'],
            'rawtherapee': ['graphics'],
            'audacity': ['multimedia'],
            'ardour': ['multimedia'],
            'musescore': ['multimedia'],
            'kdenlive': ['multimedia'],
            'shotcut': ['multimedia'],
            'obs-studio': ['multimedia'],
            'discord': ['network'],
            'telegram': ['network'],
            'signal': ['network'],
            'whatsapp': ['network'],
            'slack': ['network'],
            'teams': ['network'],
            'zoom': ['network'],
            'skype': ['network'],
            'jitsi': ['network'],
            'microsoft': ['office'],
            'adobe': ['graphics'],
            'autodesk': ['graphics'],
            'unity': ['development'],
            'unreal': ['development'],
            'godot': ['development'],
            'blender': ['graphics'],
            'maya': ['graphics'],
            '3dsmax': ['graphics'],
            'cinema4d': ['graphics'],
            'houdini': ['graphics'],
            'nuke': ['graphics'],
            'davinci': ['multimedia'],
            'premiere': ['multimedia'],
            'aftereffects': ['multimedia'],
            'photoshop': ['graphics'],
            'illustrator': ['graphics'],
            'indesign': ['office'],
            'acrobat': ['office'],
            'word': ['office'],
            'excel': ['office'],
            'powerpoint': ['office'],
            'outlook': ['office'],
            'access': ['office'],
            'publisher': ['office'],
            'onenote': ['office'],
            'teams': ['network'],
            'sharepoint': ['network'],
            'onedrive': ['network'],
            'azure': ['development'],
            'aws': ['development'],
            'google': ['network'],
            'chrome': ['network'],
            'android': ['development'],
            'ios': ['development'],
            'windows': ['system'],
            'macos': ['system'],
            'linux': ['system'],
            'ubuntu': ['system'],
            'debian': ['system'],
            'fedora': ['system'],
            'centos': ['system'],
            'redhat': ['system'],
            'suse': ['system'],
            'arch': ['system'],
            'gentoo': ['system'],
            'slackware': ['system'],
            'freebsd': ['system'],
            'netbsd': ['system'],
            'openbsd': ['system'],
            'dragonfly': ['system'],
            'haiku': ['system'],
            'reactos': ['system'],
            'qemu': ['utility'],
            'virtualbox': ['utility'],
            'vmware': ['utility'],
            'hyperv': ['utility'],
            'docker': ['development'],
            'kubernetes': ['development'],
            'ansible': ['development'],
            'puppet': ['development'],
            'chef': ['development'],
            'terraform': ['development'],
            'jenkins': ['development'],
            'gitlab': ['development'],
            'github': ['development'],
            'bitbucket': ['development'],
            'jira': ['development'],
            'confluence': ['development'],
            'trello': ['development'],
            'asana': ['development'],
            'notion': ['office'],
            'evernote': ['office'],
            'onenote': ['office'],
            'keep': ['office'],
            'todoist': ['office'],
            'things': ['office'],
            'omnifocus': ['office'],
            'bear': ['office'],
            'ulysses': ['office'],
            'scrivener': ['office'],
            'finaldraft': ['office'],
            'celtx': ['office'],
            'fadein': ['office'],
            'highland': ['office'],
            'writerduet': ['office'],
            'celtx': ['office'],
            'fadein': ['office'],
            'highland': ['office'],
            'writerduet': ['office'],
            'celtx': ['office'],
            'fadein': ['office'],
            'highland': ['office'],
            'writerduet': ['office']
        }
        mapping.update(common_mappings)
        return mapping
    
    def get_package_icon(self, package_name, size=20):
        """Get icon for a package, with fast fallback"""
        if package_name in self.icon_cache:
            return self.icon_cache[package_name]
        
        # Quick icon search - try most common patterns first
        icon = self._quick_icon_search(package_name, size)
        if icon:
            self.icon_cache[package_name] = icon
            return icon
        
        # Fallback to category-based icon
        category = self._get_package_category(package_name)
        icon = self._get_category_icon(category, size)
        if icon:
            self.icon_cache[package_name] = icon
            return icon
        
        # Final fallback to generic package icon
        fallback = self._get_fallback_icon(size)
        self.icon_cache[package_name] = fallback
        return fallback
    
    def _quick_icon_search(self, package_name, size):
        """Fast icon search without heavy file system operations"""
        # Try common icon names first
        common_icons = [
            package_name,
            package_name.lower(),
            f"{package_name}-symbolic",
            f"{package_name.lower()}-symbolic"
        ]
        
        for icon_name in common_icons:
            icon = self._load_icon_by_name(icon_name, size)
            if icon:
                return icon
        
        # Try to find desktop file icon (simplified)
        desktop_icon = self._find_desktop_icon_simple(package_name)
        if desktop_icon:
            return self._load_icon_by_name(desktop_icon, size)
        
        return None
    
    def _find_desktop_icon_simple(self, package_name):
        """Simplified desktop file icon search"""
        desktop_paths = [
            "/usr/share/applications",
            os.path.expanduser("~/.local/share/applications")
        ]
        
        for path in desktop_paths:
            if not os.path.exists(path):
                continue
            
            # Look for exact match first
            desktop_file = os.path.join(path, f"{package_name}.desktop")
            if os.path.exists(desktop_file):
                try:
                    with open(desktop_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            if line.startswith('Icon='):
                                icon_name = line.split('=', 1)[1].strip()
                                if icon_name:
                                    return icon_name
                except:
                    continue
            
            # Look for partial match (only first few files to avoid slowness)
            try:
                files = os.listdir(path)[:50]  # Limit to first 50 files
                for file in files:
                    if file.endswith('.desktop') and package_name.lower() in file.lower():
                        try:
                            with open(os.path.join(path, file), 'r', encoding='utf-8') as f:
                                for line in f:
                                    if line.startswith('Icon='):
                                        icon_name = line.split('=', 1)[1].strip()
                                        if icon_name:
                                            return icon_name
                        except:
                            continue
            except:
                continue
        
        return None
    
    def _find_package_icon_enhanced(self, package_name, size):
        """Enhanced icon search like pamac"""
        # First, try to find desktop files
        desktop_icon = self._find_desktop_file_icon(package_name)
        if desktop_icon:
            return self._load_icon_by_name(desktop_icon, size)
        
        # Try direct icon file search
        icon_file = self._find_icon_file(package_name)
        if icon_file:
            return self._load_icon_from_path(icon_file, size)
        
        # Try common icon names
        common_icons = [
            package_name,
            package_name.lower(),
            f"{package_name}-symbolic",
            f"{package_name.lower()}-symbolic",
            f"org.{package_name}.{package_name}",
            f"com.{package_name}.{package_name}",
            f"io.{package_name}.{package_name}"
        ]
        
        for icon_name in common_icons:
            icon = self._load_icon_by_name(icon_name, size)
            if icon:
                return icon
        
        return None
    
    def _find_desktop_file_icon(self, package_name):
        """Find icon from desktop files"""
        desktop_paths = [
            "/usr/share/applications",
            os.path.expanduser("~/.local/share/applications"),
            "/var/lib/flatpak/exports/share/applications",
            "/usr/share/flatpak/exports/share/applications"
        ]
        
        for path in desktop_paths:
            if not os.path.exists(path):
                continue
            
            for file in os.listdir(path):
                if file.endswith('.desktop'):
                    # Check if package name is in desktop file name
                    if package_name.lower() in file.lower():
                        try:
                            with open(os.path.join(path, file), 'r', encoding='utf-8') as f:
                                content = f.read()
                                for line in content.split('\n'):
                                    if line.startswith('Icon='):
                                        icon_name = line.split('=', 1)[1].strip()
                                        if icon_name:
                                            return icon_name
                        except:
                            continue
                    
                    # Also check inside desktop file content
                    try:
                        with open(os.path.join(path, file), 'r', encoding='utf-8') as f:
                            content = f.read()
                            if package_name.lower() in content.lower():
                                for line in content.split('\n'):
                                    if line.startswith('Icon='):
                                        icon_name = line.split('=', 1)[1].strip()
                                        if icon_name:
                                            return icon_name
                    except:
                        continue
        
        return None
    
    def _find_icon_file(self, package_name):
        """Find icon file directly"""
        icon_extensions = ['.png', '.svg', '.xpm', '.ico']
        
        for path in self.icon_paths:
            if not os.path.exists(path):
                continue
            
            # Direct match
            for ext in icon_extensions:
                icon_file = os.path.join(path, f"{package_name}{ext}")
                if os.path.exists(icon_file):
                    return icon_file
            
            # Partial match
            for file in os.listdir(path):
                if package_name.lower() in file.lower() and any(file.endswith(ext) for ext in icon_extensions):
                    return os.path.join(path, file)
        
        return None
    
    def get_better_fallback_icon(self, package_name, size=20):
        """Get a more specific fallback icon based on package name patterns"""
        # First try enhanced search
        icon = self._find_package_icon_enhanced(package_name, size)
        if icon:
            return icon
        
        name_lower = package_name.lower()
        
        # Development tools
        if any(x in name_lower for x in ['dev', 'devel', 'lib', 'gcc', 'clang', 'python', 'node', 'npm', 'yarn', 'git', 'svn', 'cmake', 'make', 'ninja']):
            return self._load_icon_by_name('applications-development-symbolic', size)
        
        # Multimedia
        if any(x in name_lower for x in ['video', 'audio', 'media', 'vlc', 'mpv', 'ffmpeg', 'gstreamer', 'pulse', 'alsa']):
            return self._load_icon_by_name('applications-multimedia-symbolic', size)
        
        # Graphics
        if any(x in name_lower for x in ['image', 'graphic', 'photo', 'draw', 'paint', 'gimp', 'inkscape', 'blender', 'krita']):
            return self._load_icon_by_name('applications-graphics-symbolic', size)
        
        # Office
        if any(x in name_lower for x in ['office', 'document', 'spreadsheet', 'presentation', 'word', 'excel', 'powerpoint', 'libreoffice']):
            return self._load_icon_by_name('applications-office-symbolic', size)
        
        # Games
        if any(x in name_lower for x in ['game', 'play', 'steam', 'wine', 'emulator']):
            return self._load_icon_by_name('applications-games-symbolic', size)
        
        # Network
        if any(x in name_lower for x in ['web', 'browser', 'http', 'ftp', 'ssh', 'vpn', 'network', 'internet']):
            return self._load_icon_by_name('applications-internet-symbolic', size)
        
        # System
        if any(x in name_lower for x in ['system', 'kernel', 'boot', 'init', 'systemd', 'udev', 'firmware']):
            return self._load_icon_by_name('system-run-symbolic', size)
        
        # Education
        if any(x in name_lower for x in ['learn', 'study', 'education', 'school', 'university', 'course']):
            return self._load_icon_by_name('applications-education-symbolic', size)
        
        # Science
        if any(x in name_lower for x in ['science', 'research', 'math', 'physics', 'chemistry', 'biology', 'statistics']):
            return self._load_icon_by_name('applications-science-symbolic', size)
        
        return self._load_icon_by_name('package-x-generic', size)
    
    def _find_package_icon(self, package_name, size):
        """Find icon for specific package"""
        # Check desktop files first
        for path in self.icon_paths:
            if os.path.exists(path):
                if path.endswith('applications'):
                    # Look for .desktop files
                    for file in os.listdir(path):
                        if file.endswith('.desktop') and package_name.lower() in file.lower():
                            try:
                                with open(os.path.join(path, file), 'r') as f:
                                    content = f.read()
                                    for line in content.split('\n'):
                                        if line.startswith('Icon='):
                                            icon_name = line.split('=', 1)[1].strip()
                                            return self._load_icon_by_name(icon_name, size)
                            except:
                                continue
                else:
                    # Look for icon files
                    for file in os.listdir(path):
                        if package_name.lower() in file.lower() and any(file.endswith(ext) for ext in ['.png', '.svg', '.xpm']):
                            try:
                                return self._load_icon_from_path(os.path.join(path, file), size)
                            except:
                                continue
        return None
    
    def _get_package_category(self, package_name):
        """Determine package category based on name and metadata"""
        # Check our mapping first
        if package_name in self.category_mapping:
            return self.category_mapping[package_name][0] if self.category_mapping[package_name] else 'utility'
        
        # Try to determine from package name
        name_lower = package_name.lower()
        
        # Development tools
        if any(x in name_lower for x in ['dev', 'devel', 'lib', 'gcc', 'clang', 'python', 'node', 'npm', 'yarn', 'git', 'svn', 'cmake', 'make', 'ninja']):
            return 'development'
        
        # Multimedia
        if any(x in name_lower for x in ['video', 'audio', 'media', 'vlc', 'mpv', 'ffmpeg', 'gstreamer', 'pulse', 'alsa']):
            return 'multimedia'
        
        # Graphics
        if any(x in name_lower for x in ['image', 'graphic', 'photo', 'draw', 'paint', 'gimp', 'inkscape', 'blender', 'krita']):
            return 'graphics'
        
        # Office
        if any(x in name_lower for x in ['office', 'document', 'spreadsheet', 'presentation', 'word', 'excel', 'powerpoint', 'libreoffice']):
            return 'office'
        
        # Games
        if any(x in name_lower for x in ['game', 'play', 'steam', 'wine', 'emulator']):
            return 'games'
        
        # Network
        if any(x in name_lower for x in ['web', 'browser', 'http', 'ftp', 'ssh', 'vpn', 'network', 'internet']):
            return 'network'
        
        # System
        if any(x in name_lower for x in ['system', 'kernel', 'boot', 'init', 'systemd', 'udev', 'firmware']):
            return 'system'
        
        # Education
        if any(x in name_lower for x in ['learn', 'study', 'education', 'school', 'university', 'course']):
            return 'education'
        
        # Science
        if any(x in name_lower for x in ['science', 'research', 'math', 'physics', 'chemistry', 'biology', 'statistics']):
            return 'science'
        
        return 'utility'
    
    def _get_category_icon(self, category, size):
        """Get icon for a category"""
        icon_name = self.default_icons.get(category, 'applications-utilities-symbolic')
        return self._load_icon_by_name(icon_name, size)
    
    def _get_fallback_icon(self, size):
        """Get fallback package icon"""
        return self._load_icon_by_name('package-x-generic', size)
    
    def _load_icon_by_name(self, icon_name, size):
        """Load icon by name using Gtk icon theme"""
        try:
            icon_theme = Gtk.IconTheme.get_default()
            icon_info = icon_theme.lookup_icon(icon_name, size, 0)
            if icon_info:
                return icon_info.load_icon()
        except:
            pass
        return None
    
    def _load_icon_from_path(self, path, size):
        """Load icon from file path"""
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, size, size)
            return pixbuf
        except:
            return None



# ----------------------------
# Enhanced Package Data Structure
# ----------------------------

class PackageInfo:
    def __init__(self, data):
        self.name = data.get('name', '')
        self.version = data.get('version', '')
        self.description = data.get('description', '')
        self.repository = data.get('repository', '')
        self.installed = data.get('installed', False)
        self.size = data.get('size', '')
        self.license = data.get('license', '')
        self.url = data.get('url', '')
        self.category = data.get('category', 'utility')
        self.icon = None
        self.dependencies = data.get('dependencies', [])
        self.required_by = data.get('required_by', [])
        self.last_updated = data.get('last_updated', '')
        self.download_size = data.get('download_size', '')
        self.build_date = data.get('build_date', '')
        self.packager = data.get('packager', '')
        self.architecture = data.get('architecture', '')
        self.reason = data.get('reason', '')  # why installed
    
    def get_display_name(self):
        """Get display name with repository prefix if needed"""
        if self.repository and self.repository != 'installed':
            return f"{self.repository}/{self.name}"
        return self.name
    
    def get_status_icon(self):
        """Get status icon based on installation state"""
        if self.installed:
            return "✅"  # Green checkmark
        return "📦"  # Package icon
    
    def get_repository_color(self):
        """Get color for repository display"""
        colors = {
            'core': '#FF6B6B',      # Red
            'extra': '#4ECDC4',     # Teal
            'community': '#45B7D1',  # Blue
            'multilib': '#96CEB4',  # Green
            'aur': '#FFEAA7',       # Yellow
            'installed': '#DDA0DD'  # Plum
        }
        return colors.get(self.repository, '#CCCCCC')

# ----------------------------
# Utility helpers
# ----------------------------

def run_command_stream(command, on_output, on_progress_hint=None, password=None):
    """Run a command and stream stdout lines via on_output callback.
    If password provided, pipe it to sudo -S.
    """
    try:
        if password and (len(command) == 0 or command[0] != 'sudo'):
            full = ["bash", "-lc", f"echo '{password}' | sudo -S " + " ".join(command)]
            proc = subprocess.Popen(
                full,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
        else:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

        while True:
            line = proc.stdout.readline()
            if line == '' and proc.poll() is not None:
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
    def __init__(self, query, search_type, on_results, icon_manager):
        super().__init__(daemon=True)
        self.query = query
        self.search_type = search_type  # 'official' | 'aur' | 'installed'
        self.on_results = on_results
        self.icon_manager = icon_manager

    def _is_installed(self, name):
        try:
            r = subprocess.run(["pacman", "-Q", name], capture_output=True, text=True)
            return r.returncode == 0
        except Exception:
            return False

    def _parse_pacman_search(self, output):
        """Parse pacman -Ss output"""
        packages = []
        current_package = None
        
        for line in output.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
                
            # Package line format: repo/name version [installed]
            if '/' in line and not line.startswith('    '):
                parts = line.split(' ', 1)
                if len(parts) >= 2:
                    repo_name = parts[0]
                    version_desc = parts[1]
                    
                    # Extract repo and name
                    if '/' in repo_name:
                        repo, name = repo_name.split('/', 1)
                    else:
                        repo = 'unknown'
                        name = repo_name
                    
                    # Extract version (remove [installed] if present)
                    version = version_desc
                    installed = False
                    if '[installed:' in version_desc:
                        # Format: version [installed: installed_version]
                        version = version_desc.split(' [installed:', 1)[0]
                        installed = True
                    elif '[installed]' in version_desc:
                        # Format: version [installed]
                        version = version_desc.replace(' [installed]', '')
                        installed = True
                    else:
                        # Just version
                        version = version_desc
                    
                    current_package = {
                        'name': name,
                        'version': version,
                        'description': '',
                        'repository': repo,
                        'installed': installed
                    }
                    packages.append(current_package)
            
            # Description line (indented)
            elif line.startswith('    ') and current_package:
                description = line.strip()
                if description and not description.startswith('['):
                    current_package['description'] = description
        
        return packages
    
    def _parse_aur_search(self, output):
        """Parse yay -Ss output"""
        packages = []
        current_package = None
        
        for line in output.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
                
            # Package line format: aur/name version (+votes -votes) [installed]
            if 'aur/' in line and not line.startswith('    '):
                parts = line.split(' ', 1)
                if len(parts) >= 2:
                    repo_name = parts[0]
                    version_desc = parts[1]
                    
                    # Extract name (remove aur/ prefix)
                    name = repo_name.replace('aur/', '')
                    
                    # Extract version (remove vote info and [installed])
                    version = version_desc
                    installed = False
                    
                    # Remove vote info like (+5 -0)
                    if '(' in version and ')' in version:
                        version = version.split('(', 1)[0].strip()
                    
                    # Check for [installed]
                    if '[installed]' in version:
                        version = version.replace(' [installed]', '')
                        installed = True
                    
                    current_package = {
                        'name': name,
                        'version': version,
                        'description': '',
                        'repository': 'aur',
                        'installed': installed
                    }
                    packages.append(current_package)
            
            # Description line (indented)
            elif line.startswith('    ') and current_package:
                description = line.strip()
                if description and not description.startswith('['):
                    current_package['description'] = description
        
        return packages
    
    def _parse_installed_search(self, output):
        """Parse pacman -Qs output"""
        packages = []
        current_package = None
        
        for line in output.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
                
            # Package line format: local/name version [installed]
            if 'local/' in line and not line.startswith('    '):
                parts = line.split(' ', 1)
                if len(parts) >= 2:
                    repo_name = parts[0]
                    version_desc = parts[1]
                    
                    # Extract name (remove local/ prefix)
                    name = repo_name.replace('local/', '')
                    
                    # Extract version
                    version = version_desc
                    installed = True  # All packages in -Qs are installed
                    
                    current_package = {
                        'name': name,
                        'version': version,
                        'description': '',
                        'repository': 'installed',
                        'installed': installed
                    }
                    packages.append(current_package)
            
            # Description line (indented)
            elif line.startswith('    ') and current_package:
                description = line.strip()
                if description and not description.startswith('['):
                    current_package['description'] = description
        
        return packages

    def run(self):
        try:
            if self.search_type == 'official':
                result = subprocess.run(["pacman", "-Ss", self.query], capture_output=True, text=True, timeout=30)
                pkgs = self._parse_pacman_search(result.stdout)
            elif self.search_type == 'aur':
                result = subprocess.run(["yay", "-Ss", self.query], capture_output=True, text=True, timeout=30)
                pkgs = self._parse_aur_search(result.stdout)
            else:
                # installed search
                result = subprocess.run(["pacman", "-Qs", self.query], capture_output=True, text=True, timeout=30)
                pkgs = []
                lines = result.stdout.split('\n')
                i = 0
                while i < len(lines):
                    line = lines[i].strip()
                    if line and not line.startswith(' '):
                        parts = line.split(' ', 1)
                        if len(parts) >= 2:
                            name = parts[1]
                            version = ''
                            try:
                                ver_result = subprocess.run(["pacman", "-Q", name], capture_output=True, text=True)
                                if ver_result.returncode == 0:
                                    version = ver_result.stdout.split(' ', 1)[1].strip()
                            except Exception:
                                pass
                            description = ''
                            if i + 1 < len(lines) and lines[i + 1].startswith('    '):
                                description = lines[i + 1].strip()
                                i += 1
                            
                            pkg_data = {
                                'name': name,
                                'version': version,
                                'description': description,
                                'repository': 'installed',
                                'installed': True
                            }
                            package = PackageInfo(pkg_data)
                            package.icon = self.icon_manager.get_package_icon(name)
                            pkgs.append(package)
                    i += 1
            GLib.idle_add(self.on_results, pkgs)
        except Exception:
            GLib.idle_add(self.on_results, [])


# ----------------------------
# Enhanced GTK UI with Icons
# ----------------------------

class PKGERWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"{APP_NAME} {APP_VERSION} - Professional Package Manager")
        self.set_default_size(800, 600)
        self.set_resizable(True)
        self.set_border_width(4)
        self.set_size_request(600, 400)  # Minimum size
        
        # Initialize icon manager
        self.icon_manager = PackageIconManager()

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

        # Apply modern styling
        self._apply_styling()

        vroot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(vroot)

        # Professional menu bar
        vroot.pack_start(self._create_menu_bar(), False, False, 0)

        # Enhanced header with branding
        vroot.pack_start(self._create_enhanced_header(), False, False, 0)

        # Search section with filters
        vroot.pack_start(self._create_enhanced_search(), False, False, 0)

        # Content area (Notebook)
        vroot.pack_start(self._create_notebook(), True, True, 0)

        # Progress section
        vroot.pack_start(self._create_progress(), False, False, 0)

        # Enhanced status bar
        vroot.pack_start(self._create_status_bar(), False, False, 0)

        # Load packages initially (async)
        GLib.idle_add(self.load_installed_packages)
        # Load repositories catalog
        GLib.idle_add(self.load_repos_data)
        # Load updates list
        GLib.idle_add(self.load_updates_data)

    def _apply_styling(self):
        """Apply modern styling to the application with GTK theme compatibility"""
        css_provider = Gtk.CssProvider()
        css = """
        /* Compact styling for 800x600 resolution */
        .package-row {
            padding: 4px;
            border-radius: 2px;
            margin: 1px;
        }
        
        .package-row:hover {
            background-color: alpha(@theme_fg_color, 0.1);
        }
        
        .package-name {
            font-weight: bold;
            font-size: 11px;
        }
        
        .package-version {
            color: alpha(@theme_fg_color, 0.7);
            font-size: 10px;
        }
        
        .package-description {
            color: alpha(@theme_fg_color, 0.8);
            font-size: 10px;
        }
        
        .repository-badge {
            background-color: #4CAF50;
            color: white;
            padding: 1px 4px;
            border-radius: 6px;
            font-size: 8px;
            font-weight: bold;
        }
        
        .aur-badge {
            background-color: #FF9800;
        }
        
        .core-badge {
            background-color: #F44336;
        }
        
        .extra-badge {
            background-color: #2196F3;
        }
        
        .community-badge {
            background-color: #4CAF50;
        }
        
        .installed-badge {
            background-color: #9C27B0;
        }
        
        .search-entry {
            border-radius: 12px;
            padding: 4px 8px;
            font-size: 11px;
        }
        
        /* Compact colored buttons */
        .primary-button {
            background-color: #2196F3;
            color: white;
            border-radius: 3px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 10px;
        }
        
        .success-button {
            background-color: #4CAF50;
            color: white;
            border-radius: 3px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 10px;
        }
        
        .warning-button {
            background-color: #FF9800;
            color: white;
            border-radius: 3px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 10px;
        }
        
        .danger-button {
            background-color: #F44336;
            color: white;
            border-radius: 3px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 10px;
        }
        
        .info-button {
            background-color: #00BCD4;
            color: white;
            border-radius: 3px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 10px;
        }
        
        .package-icon {
            border-radius: 2px;
            padding: 1px;
        }
        
        .status-bar {
            background-color: @theme_bg_color;
            border-top: 1px solid @borders;
            padding: 2px 4px;
            font-size: 10px;
        }
        
        .header-section {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 8px;
            border-radius: 4px;
            margin-bottom: 8px;
        }
        
        .stats-box {
            background-color: rgba(255, 255, 255, 0.1);
            border-radius: 3px;
            padding: 4px 6px;
            margin: 0 2px;
        }
        
        .package-list {
            border: 1px solid @borders;
            border-radius: 3px;
            background-color: @theme_bg_color;
        }
        
        .package-list row:hover {
            background-color: alpha(@theme_fg_color, 0.05);
        }
        
        .package-list row:selected {
            background-color: @theme_selected_bg_color;
        }
        
        .action-panel {
            background-color: @theme_bg_color;
            border: 1px solid @borders;
            border-radius: 3px;
            padding: 6px;
        }
        
        .output-panel {
            background-color: @theme_bg_color;
            border: 1px solid @borders;
            border-radius: 3px;
            padding: 4px;
        }
        
        .progress-bar {
            border-radius: 6px;
            background-color: @theme_bg_color;
        }
        
        .progress-bar progress {
            border-radius: 6px;
            background-color: #2196F3;
        }
        
        /* Compact layout for small screens */
        .compact-spacing {
            margin: 1px;
            padding: 2px;
        }
        
        .compact-button {
            padding: 2px 6px;
            font-size: 9px;
        }
        
        /* Smaller tree view */
        treeview {
            font-size: 10px;
        }
        
        /* Smaller notebook tabs */
        notebook tab {
            padding: 4px 8px;
            font-size: 10px;
        }
        
        /* Smaller frame labels */
        frame > label {
            font-size: 10px;
        }
        """
        css_provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _create_enhanced_header(self):
        """Create enhanced header with compact design for small screens"""
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Top row: Title and branding with gradient background
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_row.get_style_context().add_class("header-section")
        
        # App icon and title - smaller
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        try:
            app_icon = Gtk.Image.new_from_icon_name("applications-system", Gtk.IconSize.SMALL_TOOLBAR)
            title_box.pack_start(app_icon, False, False, 0)
        except:
            pass
        
        title_label = Gtk.Label()
        title_label.set_markup(f"<span size='large' weight='bold'>{APP_NAME}</span> <span size='medium'>{APP_VERSION}</span>")
        title_box.pack_start(title_label, False, False, 0)
        
        subtitle = Gtk.Label()
        subtitle.set_markup("<span size='small'>Professional Package Manager</span>")
        subtitle.set_xalign(0.0)
        title_box.pack_start(subtitle, False, False, 0)
        
        top_row.pack_start(title_box, True, True, 0)
        
        # Quick stats with compact styling
        stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        self.packages_count_label = Gtk.Label()
        self.packages_count_label.set_markup("<span size='small'><b>📦:</b> 0</span>")
        self.packages_count_label.get_style_context().add_class("stats-box")
        stats_box.pack_start(self.packages_count_label, False, False, 0)
        
        self.updates_count_label = Gtk.Label()
        self.updates_count_label.set_markup("<span size='small'><b>🔄:</b> 0</span>")
        self.updates_count_label.get_style_context().add_class("stats-box")
        stats_box.pack_start(self.updates_count_label, False, False, 0)
        
        self.repos_count_label = Gtk.Label()
        self.repos_count_label.set_markup("<span size='small'><b>🏪:</b> 0</span>")
        self.repos_count_label.get_style_context().add_class("stats-box")
        stats_box.pack_start(self.repos_count_label, False, False, 0)
        
        top_row.pack_start(stats_box, False, False, 0)
        
        header_box.pack_start(top_row, False, False, 0)
        
        # Bottom row: Action buttons with compact styling
        bottom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        
        self.update_btn = Gtk.Button.new_with_label("🔄 Update")
        self.update_btn.get_style_context().add_class("action-button")
        self.update_btn.get_style_context().add_class("primary-button")
        self.update_btn.set_size_request(-1, 28)
        self.update_btn.connect("clicked", lambda *_: self.update_system())

        self.clean_btn = Gtk.Button.new_with_label("🧹 Clean")
        self.clean_btn.get_style_context().add_class("action-button")
        self.clean_btn.get_style_context().add_class("warning-button")
        self.clean_btn.set_size_request(-1, 28)
        self.clean_btn.connect("clicked", lambda *_: self.clean_cache())

        self.browse_btn = Gtk.Button.new_with_label("📁 Browse")
        self.browse_btn.get_style_context().add_class("action-button")
        self.browse_btn.get_style_context().add_class("info-button")
        self.browse_btn.set_size_request(-1, 28)
        self.browse_btn.set_tooltip_text("Install local .pkg.tar.zst/.pkg.tar.xz package files")
        self.browse_btn.connect("clicked", lambda *_: self.browse_local_package())

        self.refresh_all_btn = Gtk.Button.new_with_label("🔄 Refresh")
        self.refresh_all_btn.get_style_context().add_class("action-button")
        self.refresh_all_btn.get_style_context().add_class("success-button")
        self.refresh_all_btn.set_size_request(-1, 28)
        self.refresh_all_btn.set_tooltip_text("Refresh packages, repos and views")
        self.refresh_all_btn.connect("clicked", lambda *_: self.refresh_all())

        self.reset_sort_btn = Gtk.Button.new_with_label("📊 Reset Sort")
        self.reset_sort_btn.get_style_context().add_class("action-button")
        self.reset_sort_btn.get_style_context().add_class("info-button")
        self.reset_sort_btn.set_size_request(-1, 28)
        self.reset_sort_btn.set_tooltip_text("Reset sorting to default (Package Name A-Z)")
        self.reset_sort_btn.connect("clicked", lambda *_: self.reset_sorting())

        for b in (self.update_btn, self.clean_btn, self.browse_btn, self.refresh_all_btn, self.reset_sort_btn):
            bottom_row.pack_start(b, False, False, 0)

        header_box.pack_start(bottom_row, False, False, 0)
        
        return header_box

    def _create_enhanced_search(self):
        """Create enhanced search with compact design for small screens"""
        search_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Search bar with compact styling
        search_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_bar.get_style_context().add_class("search-panel")
        
        # Search icon
        try:
            search_icon = Gtk.Image.new_from_icon_name("system-search", Gtk.IconSize.SMALL_TOOLBAR)
            search_bar.pack_start(search_icon, False, False, 0)
        except:
            pass
        
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("🔍 Search packages...")
        self.search_entry.get_style_context().add_class("search-entry")
        self.search_entry.set_size_request(200, -1)
        self.search_entry.connect("activate", lambda *_: self.search_packages())
        search_bar.pack_start(self.search_entry, True, True, 0)

        self.search_type = Gtk.ComboBoxText()
        self.search_type.append_text("🔍 All")
        self.search_type.append_text("📦 Official")
        self.search_type.append_text("⭐ AUR")
        self.search_type.append_text("💾 Installed")
        self.search_type.set_active(0)

        self.search_btn = Gtk.Button.new_with_label("Search")
        self.search_btn.get_style_context().add_class("action-button")
        self.search_btn.get_style_context().add_class("primary-button")
        self.search_btn.set_size_request(-1, 28)
        self.search_btn.connect("clicked", lambda *_: self.search_packages())

        search_bar.pack_start(self.search_type, False, False, 0)
        search_bar.pack_start(self.search_btn, False, False, 0)
        
        search_box.pack_start(search_bar, False, False, 0)
        
        return search_box

    def _create_progress(self):
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        self.progress_label = Gtk.Label(label="Ready")
        self.progress_label.set_xalign(0.0)
        v.pack_start(self.progress_label, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_fraction(0.0)
        self.progress.set_no_show_all(True)
        self.progress.set_size_request(-1, 16)
        v.pack_start(self.progress, False, False, 0)

        return v

    def _create_status_bar(self):
        """Create enhanced status bar with multiple indicators"""
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        # Status message
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_xalign(0.0)
        status_box.pack_start(self.status_label, True, True, 0)
        
        # System info
        try:
            # Show current user
            user = os.getenv('USER', 'unknown')
            user_label = Gtk.Label(label=f"👤 {user}")
            user_label.set_xalign(1.0)
            status_box.pack_start(user_label, False, False, 0)
            
            # Show system architecture
            arch = os.uname().machine if hasattr(os, 'uname') else 'unknown'
            arch_label = Gtk.Label(label=f"🏗️ {arch}")
            arch_label.set_xalign(1.0)
            status_box.pack_start(arch_label, False, False, 0)
        except:
            pass
        
        return status_box

    # UI Builders
    def _create_menu_bar(self):
        menubar = Gtk.MenuBar()

        # File Menu
        file_menu_item = Gtk.MenuItem(label="File")
        menubar.add(file_menu_item)

        file_menu = Gtk.Menu()
        file_menu_item.set_submenu(file_menu)

        # Package Management
        refresh_item = Gtk.MenuItem(label="🔄 Refresh Package List")
        refresh_item.connect("activate", lambda *_: self.refresh_packages())
        file_menu.append(refresh_item)

        refresh_all_item = Gtk.MenuItem(label="🔄 Refresh All Data")
        refresh_all_item.connect("activate", lambda *_: self.refresh_all())
        file_menu.append(refresh_all_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        # Local Package Management
        browse_item = Gtk.MenuItem(label="📁 Browse Local Package...")
        browse_item.connect("activate", lambda *_: self.browse_local_package())
        file_menu.append(browse_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        # Export
        export_list_item = Gtk.MenuItem(label="💾 Export Package List...")
        export_list_item.connect("activate", lambda *_: self._export_package_list())
        file_menu.append(export_list_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        # Exit
        exit_item = Gtk.MenuItem(label="❌ Exit")
        exit_item.connect("activate", lambda *_: self.close())
        file_menu.append(exit_item)

        # Tools Menu
        tools_menu_item = Gtk.MenuItem(label="Tools")
        menubar.add(tools_menu_item)

        tools_menu = Gtk.Menu()
        tools_menu_item.set_submenu(tools_menu)

        # System Management
        update_item = Gtk.MenuItem(label="🔄 Update System")
        update_item.connect("activate", lambda *_: self.update_system())
        tools_menu.append(update_item)

        clean_item = Gtk.MenuItem(label="🧹 Clean Package Cache")
        clean_item.connect("activate", lambda *_: self.clean_cache())
        tools_menu.append(clean_item)

        clean_orphans_item = Gtk.MenuItem(label="🗑️ Remove Orphaned Packages")
        clean_orphans_item.connect("activate", lambda *_: self._remove_orphans())
        tools_menu.append(clean_orphans_item)

        fix_item = Gtk.MenuItem(label="🔧 Fix Broken Dependencies")
        fix_item.connect("activate", lambda *_: self.fix_broken_dependencies())
        tools_menu.append(fix_item)

        tools_menu.append(Gtk.SeparatorMenuItem())

        # Package Operations
        install_selected_item = Gtk.MenuItem(label="📥 Install Selected Package")
        install_selected_item.connect("activate", lambda *_: self.install_package())
        tools_menu.append(install_selected_item)

        remove_selected_item = Gtk.MenuItem(label="🗑️ Remove Selected Package")
        remove_selected_item.connect("activate", lambda *_: self.remove_package())
        tools_menu.append(remove_selected_item)

        reinstall_selected_item = Gtk.MenuItem(label="🔄 Reinstall Selected Package")
        reinstall_selected_item.connect("activate", lambda *_: self.reinstall_package())
        tools_menu.append(reinstall_selected_item)

        tools_menu.append(Gtk.SeparatorMenuItem())

        # System Information
        system_info_item = Gtk.MenuItem(label="💻 System Information")
        system_info_item.connect("activate", lambda *_: self._show_system_info())
        tools_menu.append(system_info_item)

        package_stats_item = Gtk.MenuItem(label="📊 Package Statistics")
        package_stats_item.connect("activate", lambda *_: self._show_package_stats())
        tools_menu.append(package_stats_item)

        # Help Menu
        help_menu_item = Gtk.MenuItem(label="Help")
        menubar.add(help_menu_item)

        help_menu = Gtk.Menu()
        help_menu_item.set_submenu(help_menu)

        # Documentation
        user_guide_item = Gtk.MenuItem(label="📖 User Guide")
        user_guide_item.connect("activate", lambda *_: self._show_user_guide())
        help_menu.append(user_guide_item)

        keyboard_shortcuts_item = Gtk.MenuItem(label="⌨️ Keyboard Shortcuts")
        keyboard_shortcuts_item.connect("activate", lambda *_: self._show_keyboard_shortcuts())
        help_menu.append(keyboard_shortcuts_item)

        help_menu.append(Gtk.SeparatorMenuItem())

        # About
        about_item = Gtk.MenuItem(label="ℹ️ About PKGER")
        about_item.connect("activate", lambda *_: self.show_about())
        help_menu.append(about_item)

        return menubar

    def _create_notebook(self):
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.notebook.set_tab_pos(Gtk.PositionType.TOP)

        packages_page = self._create_packages_page()
        self.notebook.append_page(packages_page, Gtk.Label(label="Packages"))

        details_page = self._create_details_page()
        self.notebook.append_page(details_page, Gtk.Label(label="Details"))

        repos_page = self._create_repos_page()
        self.notebook.append_page(repos_page, Gtk.Label(label="Repositories"))

        updates_page = self._create_updates_page()
        self.notebook.append_page(updates_page, Gtk.Label(label="Updates"))

        return self.notebook

    def _create_updates_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.upd_select_all = Gtk.CheckButton(label="Select All")
        self.upd_select_all.connect("toggled", self._updates_toggle_all)
        self.upd_refresh_btn = Gtk.Button.new_with_label("Refresh Updates")
        self.upd_refresh_btn.connect("clicked", lambda *_: self.load_updates_data())
        self.upd_apply_btn = Gtk.Button.new_with_label("Apply Selected")
        self.upd_apply_btn.connect("clicked", self._updates_apply_selected)
        self.orphans_btn = Gtk.Button.new_with_label("Remove Orphans")
        self.orphans_btn.connect("clicked", self._remove_orphans)
        for w in (self.upd_select_all, self.upd_refresh_btn, self.upd_apply_btn, self.orphans_btn):
            toolbar.pack_start(w, False, False, 0)
        vbox.pack_start(toolbar, False, False, 0)

        self.upd_store = Gtk.ListStore(bool, str, str)  # selected, name, version->new
        self.upd_view = Gtk.TreeView(model=self.upd_store)
        tgl = Gtk.CellRendererToggle(); tgl.set_activatable(True); tgl.connect("toggled", self._updates_toggle_row)
        colsel = Gtk.TreeViewColumn("Select", tgl, active=0)
        rname = Gtk.CellRendererText(); colname = Gtk.TreeViewColumn("Package", rname, text=1)
        rver = Gtk.CellRendererText(); colver = Gtk.TreeViewColumn("Update", rver, text=2)
        for c in (colsel, colname, colver):
            self.upd_view.append_column(c)
        sw = Gtk.ScrolledWindow(); sw.add(self.upd_view)
        vbox.pack_start(sw, True, True, 0)
        return vbox

    def _create_packages_page(self):
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        # Left: package list with icons
        left_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        
        # Compact header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        list_label = Gtk.Label()
        list_label.set_markup("<span size='medium' weight='bold'>📦 Packages</span>")
        list_label.set_xalign(0.0)
        header_box.pack_start(list_label, True, True, 0)
        
        # Package count with icon
        self.count_label = Gtk.Label()
        self.count_label.set_markup("<span size='x-small' foreground='#666666'>0 packages</span>")
        self.count_label.set_xalign(1.0)
        header_box.pack_start(self.count_label, False, False, 0)
        
        left_v.pack_start(header_box, False, False, 0)

        # Enhanced store with icon column
        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str, bool)  # icon, name, version, description, repository, installed
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(True)
        self.tree.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)
        self.tree.get_style_context().add_class("package-list")
        self.tree.connect("row-activated", self.on_row_activated)
        self.tree.get_selection().connect("changed", self.on_selection_changed)

        # Icon column with compact styling
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_renderer.set_property("xpad", 2)
        icon_renderer.set_property("ypad", 1)
        icon_col = Gtk.TreeViewColumn("", icon_renderer, pixbuf=0)
        icon_col.set_min_width(24)
        icon_col.set_max_width(48)
        icon_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        icon_col.set_resizable(True)
        icon_col.set_title("")
        
        # Name column with compact styling
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("weight", 600)  # Bold
        name_renderer.set_property("xpad", 4)
        name_renderer.set_property("ypad", 2)
        name_col = Gtk.TreeViewColumn("📦 Package", name_renderer, text=1)
        name_col.set_min_width(100)
        name_col.set_max_width(400)
        name_col.set_sizing(Gtk.TreeViewColumnSizing.GROW_ONLY)
        name_col.set_resizable(True)
        name_col.set_expand(True)
        name_col.set_sort_column_id(1)
        
        # Version column with compact styling
        version_renderer = Gtk.CellRendererText()
        version_renderer.set_property("foreground", "#666666")
        version_renderer.set_property("xpad", 4)
        version_renderer.set_property("ypad", 2)
        version_col = Gtk.TreeViewColumn("🔢 Version", version_renderer, text=2)
        version_col.set_min_width(60)
        version_col.set_max_width(150)
        version_col.set_sizing(Gtk.TreeViewColumnSizing.GROW_ONLY)
        version_col.set_resizable(True)
        version_col.set_sort_column_id(2)
        
        # Repository column with color coding and compact styling
        repo_renderer = Gtk.CellRendererText()
        repo_renderer.set_property("xpad", 4)
        repo_renderer.set_property("ypad", 2)
        repo_col = Gtk.TreeViewColumn("🏪 Repository", repo_renderer, text=4)
        repo_col.set_min_width(50)
        repo_col.set_max_width(120)
        repo_col.set_sizing(Gtk.TreeViewColumnSizing.GROW_ONLY)
        repo_col.set_resizable(True)
        repo_col.set_sort_column_id(4)
        repo_col.set_cell_data_func(repo_renderer, self._render_repository_column)
        
        # Installed column with status icons and compact styling
        installed_renderer = Gtk.CellRendererText()
        installed_renderer.set_property("xpad", 4)
        installed_renderer.set_property("ypad", 2)
        installed_col = Gtk.TreeViewColumn("✅ Status", installed_renderer, text=5)
        installed_col.set_min_width(40)
        installed_col.set_max_width(100)
        installed_col.set_sizing(Gtk.TreeViewColumnSizing.GROW_ONLY)
        installed_col.set_resizable(True)
        installed_col.set_sort_column_id(5)
        installed_col.set_cell_data_func(installed_renderer, self._render_installed_column)

        for c in (icon_col, name_col, version_col, repo_col, installed_col):
            self.tree.append_column(c)

        # Add scrolling with compact styling
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        sw.add(self.tree)
        left_v.pack_start(sw, True, True, 0)

        hbox.pack_start(left_v, True, True, 0)

        # Right: actions + output with compact styling
        right_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # Actions section with compact styling
        actions_frame = Gtk.Frame(label="Actions")
        actions_frame.set_label_align(0.0, 0.5)
        actions_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        actions_frame.add(actions_v)

        self.install_btn = Gtk.Button.new_with_label("📥 Install")
        self.install_btn.set_sensitive(False)
        self.install_btn.get_style_context().add_class("success-button")
        self.install_btn.connect("clicked", lambda *_: self.install_package())

        self.remove_btn = Gtk.Button.new_with_label("🗑️ Remove")
        self.remove_btn.set_sensitive(False)
        self.remove_btn.get_style_context().add_class("danger-button")
        self.remove_btn.connect("clicked", lambda *_: self.remove_package())

        self.reinstall_btn = Gtk.Button.new_with_label("🔄 Reinstall")
        self.reinstall_btn.set_sensitive(False)
        self.reinstall_btn.get_style_context().add_class("warning-button")
        self.reinstall_btn.connect("clicked", lambda *_: self.reinstall_package())

        for b in (self.install_btn, self.remove_btn, self.reinstall_btn):
            b.set_size_request(-1, 28)
            actions_v.pack_start(b, False, False, 0)

        right_v.pack_start(actions_frame, False, False, 0)

        # Output section with compact styling
        output_frame = Gtk.Frame(label="Output Log")
        output_frame.set_label_align(0.0, 0.5)
        output_v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        output_frame.add(output_v)

        self.textbuf = Gtk.TextBuffer()
        self.textview = Gtk.TextView(buffer=self.textbuf)
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_size_request(-1, 120)

        out_sw = Gtk.ScrolledWindow()
        out_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        out_sw.set_min_content_height(120)
        out_sw.set_shadow_type(Gtk.ShadowType.IN)
        out_sw.add(self.textview)
        output_v.pack_start(out_sw, True, True, 0)

        right_v.pack_start(output_frame, True, True, 0)

        hbox.pack_start(right_v, False, False, 0)

        return hbox

    def _create_details_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        outer.get_style_context().add_class("details-panel")

        # Header with all buttons
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header_label = Gtk.Label()
        header_label.set_markup("<span size='medium' weight='bold'>📋 Package Details</span>")
        header_box.pack_start(header_label, True, True, 0)
        
        # Action buttons row
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        
        # Refresh button
        refresh_details_btn = Gtk.Button.new_with_label("🔄 Refresh")
        refresh_details_btn.get_style_context().add_class("action-button")
        refresh_details_btn.get_style_context().add_class("info-button")
        refresh_details_btn.set_size_request(-1, 28)
        refresh_details_btn.connect("clicked", self._refresh_details)
        buttons_box.pack_start(refresh_details_btn, False, False, 0)
        
        # Copy Details button
        copy_btn = Gtk.Button.new_with_label("📋 Copy")
        copy_btn.get_style_context().add_class("action-button")
        copy_btn.get_style_context().add_class("info-button")
        copy_btn.set_size_request(-1, 28)
        copy_btn.connect("clicked", self._copy_details)
        buttons_box.pack_start(copy_btn, False, False, 0)
        
        # Export Details button
        export_btn = Gtk.Button.new_with_label("💾 Export")
        export_btn.get_style_context().add_class("action-button")
        export_btn.get_style_context().add_class("success-button")
        export_btn.set_size_request(-1, 28)
        export_btn.connect("clicked", self._export_details)
        buttons_box.pack_start(export_btn, False, False, 0)
        
        # Open Homepage button
        open_btn = Gtk.Button.new_with_label("🌐 Open")
        open_btn.get_style_context().add_class("action-button")
        open_btn.get_style_context().add_class("primary-button")
        open_btn.set_size_request(-1, 28)
        open_btn.connect("clicked", self._open_homepage)
        buttons_box.pack_start(open_btn, False, False, 0)
        
        # Load Dependencies button
        load_deps_btn = Gtk.Button.new_with_label("🔗 Load Deps")
        load_deps_btn.get_style_context().add_class("action-button")
        load_deps_btn.get_style_context().add_class("warning-button")
        load_deps_btn.set_size_request(-1, 28)
        load_deps_btn.connect("clicked", self._load_current_deps)
        buttons_box.pack_start(load_deps_btn, False, False, 0)
        
        # Install Package button
        install_btn = Gtk.Button.new_with_label("📥 Install")
        install_btn.get_style_context().add_class("action-button")
        install_btn.get_style_context().add_class("success-button")
        install_btn.set_size_request(-1, 28)
        install_btn.connect("clicked", self._install_from_details)
        buttons_box.pack_start(install_btn, False, False, 0)
        
        # Remove Package button
        remove_btn = Gtk.Button.new_with_label("🗑️ Remove")
        remove_btn.get_style_context().add_class("action-button")
        remove_btn.get_style_context().add_class("danger-button")
        remove_btn.set_size_request(-1, 28)
        remove_btn.connect("clicked", self._remove_from_details)
        buttons_box.pack_start(remove_btn, False, False, 0)
        
        header_box.pack_start(buttons_box, False, False, 0)
        outer.pack_start(header_box, False, False, 0)

        # Main details grid
        grid = Gtk.Grid(column_spacing=8, row_spacing=4)
        grid.set_column_homogeneous(False)
        grid.set_margin_start(4)
        grid.set_margin_end(4)

        def add_row(r, label_text, icon=""):
            lbl = Gtk.Label()
            lbl.set_markup(f"<span weight='bold' size='small'>{icon} {label_text}:</span>")
            lbl.set_xalign(0.0)
            val = Gtk.Label(label="-")
            val.set_xalign(0.0)
            val.set_line_wrap(True)
            val.set_line_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(val, 1, r, 1, 1)
            return val

        self.d_name = add_row(0, "Name", "📦")
        self.d_version = add_row(1, "Version", "🔢")
        self.d_repo = add_row(2, "Repository", "🏪")
        self.d_installed = add_row(3, "Installed", "✅")
        self.d_size = add_row(4, "Installed Size", "💾")
        self.d_license = add_row(5, "License", "📜")
        self.d_url = add_row(6, "URL", "🌐")

        outer.pack_start(grid, False, False, 0)

        # Description section
        desc_frame = Gtk.Frame(label="📝 Description")
        desc_frame.set_label_align(0.0, 0.5)
        desc_frame.get_style_context().add_class("info-panel")
        
        desc_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        desc_frame.add(desc_vbox)

        self.desc_buf = Gtk.TextBuffer()
        self.desc_view = Gtk.TextView(buffer=self.desc_buf)
        self.desc_view.set_editable(False)
        self.desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.desc_view.set_size_request(-1, 80)
        
        desc_sw = Gtk.ScrolledWindow()
        desc_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        desc_sw.add(self.desc_view)
        desc_vbox.pack_start(desc_sw, True, True, 0)
        
        outer.pack_start(desc_frame, False, False, 0)

        # Dependencies section
        deps_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        # Dependencies
        deps_frame = Gtk.Frame(label="🔗 Dependencies")
        deps_frame.set_label_align(0.0, 0.5)
        deps_frame.get_style_context().add_class("info-panel")
        
        deps_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        deps_frame.add(deps_vbox)
        
        self.deps_buf = Gtk.TextBuffer()
        deps_view = Gtk.TextView(buffer=self.deps_buf)
        deps_view.set_editable(False)
        deps_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        deps_view.set_size_request(-1, 100)
        
        deps_sw = Gtk.ScrolledWindow()
        deps_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        deps_sw.add(deps_view)
        deps_vbox.pack_start(deps_sw, True, True, 0)
        
        deps_hbox.pack_start(deps_frame, True, True, 0)

        # Required By
        rdeps_frame = Gtk.Frame(label="📋 Required By")
        rdeps_frame.set_label_align(0.0, 0.5)
        rdeps_frame.get_style_context().add_class("info-panel")
        
        rdeps_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rdeps_frame.add(rdeps_vbox)
        
        self.rdeps_buf = Gtk.TextBuffer()
        rdeps_view = Gtk.TextView(buffer=self.rdeps_buf)
        rdeps_view.set_editable(False)
        rdeps_view.set_wrap_mode(Gtk.WrapMode.CHAR)
        rdeps_view.set_size_request(-1, 100)
        
        rdeps_sw = Gtk.ScrolledWindow()
        rdeps_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        rdeps_sw.add(rdeps_view)
        rdeps_vbox.pack_start(rdeps_sw, True, True, 0)
        
        deps_hbox.pack_start(rdeps_frame, True, True, 0)

        outer.pack_start(deps_hbox, True, True, 0)

        return outer

    def _create_repos_page(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Toolbar: search/filter + actions
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.repo_search = Gtk.Entry()
        self.repo_search.set_placeholder_text("Filter packages...")
        self.repo_search.connect("activate", lambda *_: self._filter_repos_tree())
        self.repo_filter_installed = Gtk.CheckButton(label="Installed only")
        self.repo_filter_installed.connect("toggled", lambda *_: self._filter_repos_tree())
        self.repo_show_all = Gtk.CheckButton(label="Show All")
        self.repo_show_all.connect("toggled", lambda *_: self._filter_repos_tree())
        self.repo_install_btn = Gtk.Button.new_with_label("Install Selected")
        self.repo_install_btn.connect("clicked", self._repos_install_selected)
        self.repo_remove_btn = Gtk.Button.new_with_label("Remove Selected")
        self.repo_remove_btn.connect("clicked", self._repos_remove_selected)
        self.repo_export_btn = Gtk.Button.new_with_label("Export List")
        self.repo_export_btn.connect("clicked", self._repos_export_list)
        for w in (self.repo_search, self.repo_filter_installed, self.repo_show_all, self.repo_install_btn, self.repo_remove_btn, self.repo_export_btn):
            toolbar.pack_start(w, False, False, 0)
        vbox.pack_start(toolbar, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Left: vertical repos list
        self.repos_list = Gtk.ListStore(str)  # repo name
        self.repos_view = Gtk.TreeView(model=self.repos_list)
        self.repos_view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        rrepo = Gtk.CellRendererText()
        colrepo = Gtk.TreeViewColumn("Repositories", rrepo, text=0)
        self.repos_view.append_column(colrepo)
        self.repos_view.get_selection().connect("changed", self._on_repo_selected)
        swl = Gtk.ScrolledWindow(); swl.set_min_content_width(140); swl.add(self.repos_view)
        hbox.pack_start(swl, False, False, 0)

        # Right: vertical paned with table top and details bottom
        right_paned = Gtk.VPaned()

        # Packages table with checkboxes
        self.repo_pkgs_store = Gtk.ListStore(bool, str, bool)  # selected, package name, installed
        self.repo_pkgs_view = Gtk.TreeView(model=self.repo_pkgs_store)
        self.repo_pkgs_view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self.repo_pkgs_view.get_selection().connect("changed", self._on_repo_pkg_selection)

        rtoggle = Gtk.CellRendererToggle(); rtoggle.set_activatable(True); rtoggle.connect("toggled", self._on_repo_pkg_toggled)
        colsel = Gtk.TreeViewColumn("Select", rtoggle, active=0)
        rname = Gtk.CellRendererText(); colname = Gtk.TreeViewColumn("Package", rname, text=1)
        rinst = Gtk.CellRendererText(); colinst = Gtk.TreeViewColumn("Installed", rinst)
        colinst.set_cell_data_func(rinst, lambda c, cell, m, itr, d=None: cell.set_property('text', 'Yes' if m[itr][2] else 'No'))
        for c in (colsel, colname, colinst):
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

    def _create_status_bar(self):
        """Create enhanced status bar with multiple indicators"""
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        # Status message
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.set_xalign(0.0)
        status_box.pack_start(self.status_label, True, True, 0)
        
        # System info
        try:
            # Show current user
            user = os.getenv('USER', 'unknown')
            user_label = Gtk.Label(label=f"👤 {user}")
            user_label.set_xalign(1.0)
            status_box.pack_start(user_label, False, False, 0)
            
            # Show system architecture
            arch = os.uname().machine if hasattr(os, 'uname') else 'unknown'
            arch_label = Gtk.Label(label=f"🏗️ {arch}")
            arch_label.set_xalign(1.0)
            status_box.pack_start(arch_label, False, False, 0)
        except:
            pass
        
        return status_box

    # Output helpers
    def append_output(self, text):
        iter_end = self.textbuf.get_end_iter()
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.textbuf.insert(iter_end, f"[{timestamp}] {text}\n")
        mark = self.textbuf.create_mark(None, self.textbuf.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

    def set_status(self, message):
        self.status_label.set_text(message)
        self.progress_label.set_text(message)

    def set_progress_value(self, value):
        if value <= 0:
            self.progress.hide()
        else:
            self.progress.show()
            self.progress.set_fraction(max(0.0, min(1.0, value / 100.0)))
            self.progress.set_text(f"{int(value)}%")

    # Data & UI updates
    def update_package_list(self, packages):
        self.packages_cache = packages
        self.store.clear()
        
        # Process packages in batches for better performance
        batch_size = 50
        for i in range(0, len(packages), batch_size):
            batch = packages[i:i + batch_size]
            for pkg in batch:
                # Handle both dict and PackageInfo objects
                if hasattr(pkg, 'name'):
                    # PackageInfo object
                    name = pkg.name
                    version = pkg.version
                    repo = pkg.repository
                    installed = bool(pkg.installed)
                    description = pkg.description
                    icon = pkg.icon if hasattr(pkg, 'icon') and pkg.icon else None
                else:
                    # Dict object (fallback)
                    name = pkg.get('name', '')
                    version = pkg.get('version', '')
                    repo = pkg.get('repository', '')
                    installed = bool(pkg.get('installed', False))
                    description = pkg.get('description', '')
                    icon = None
                
                # Get icon from enhanced icon manager (simplified)
                if not icon:
                    icon = self.icon_manager.get_package_icon(name, 20)
                
                # Store: icon, name, version, description, repository, installed
                self.store.append([icon, name, version, description, repo, installed])
            
            # Update UI periodically
            if i % (batch_size * 2) == 0:
                GLib.idle_add(self._update_package_counters)
        
        # Final updates
        GLib.idle_add(self._update_package_counters)
        GLib.idle_add(self._apply_package_styling)
        GLib.idle_add(lambda: self.count_label.set_markup(f"<span size='small' foreground='#666666'>{len(packages)} packages</span>"))
        
        # rebuild Repositories tree (simplified)
        if hasattr(self, 'repo_store'):
            self.repo_store.clear()
            groups = {}
            for p in packages:
                if hasattr(p, 'repository'):
                    r = p.repository
                    pkg_name = p.name
                else:
                    r = p.get('repository', 'unknown')
                    pkg_name = p.get('name', '')
                groups.setdefault(r, []).append(pkg_name)
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
            return
        # New column indices: icon(0), name(1), version(2), description(3), repository(4), installed(5)
        installed = model[itr][5]
        self.install_btn.set_sensitive(not installed)
        self.remove_btn.set_sensitive(installed)
        self.reinstall_btn.set_sensitive(installed)
        name = model[itr][1]  # name is now at index 1
        repo = model[itr][4]  # repository is now at index 4
        self.fetch_and_show_details(name, repo)

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
        try:
            qi = subprocess.run(["pacman", "-Qi", name], capture_output=True, text=True, timeout=5)
            if qi.returncode == 0:
                info['Installed'] = 'Yes'
                self._parse_key_values(qi.stdout, info)
            else:
                if repo == 'aur':
                    si = subprocess.run(["yay", "-Si", name], capture_output=True, text=True, timeout=8)
                else:
                    si = subprocess.run(["pacman", "-Si", name], capture_output=True, text=True, timeout=8)
                if si.returncode == 0:
                    self._parse_key_values(si.stdout, info)
        except Exception:
            pass
        if with_deps:
            try:
                dep = subprocess.run(["pactree", name], capture_output=True, text=True, timeout=5)
                if dep.returncode == 0:
                    deps = [l.strip() for l in dep.stdout.split('\n') if l.strip()]
                    info['Depends'] = deps[:50]
            except Exception:
                pass
            try:
                rdep = subprocess.run(["pactree", "-r", name], capture_output=True, text=True, timeout=5)
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
        # New column indices: icon(0), name(1), version(2), description(3), repository(4), installed(5)
        name = model[itr][1]  # name is now at index 1
        repo = model[itr][4]  # repository is now at index 4
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
        # New column indices: icon(0), name(1), version(2), description(3), repository(4), installed(5)
        name = model[itr][1]  # name is now at index 1
        repo = model[itr][4]  # repository is now at index 4
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Installation", f"Are you sure you want to install '{name}'?"):
            return
        if repo == 'aur':
            cmd = ["yay", "-S", name, "--noconfirm"]
        else:
            # when searching official, name may be repo/name; pacman accepts that too
            cmd = ["pacman", "-S", name, "--noconfirm"]
        self.run_package_operation(cmd, "install")

    def remove_package(self):
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        # New column indices: icon(0), name(1), version(2), description(3), repository(4), installed(5)
        name = model[itr][1]  # name is now at index 1
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Removal", f"Are you sure you want to remove '{name}'?"):
            return
        cmd = ["pacman", "-R", name, "--noconfirm"]
        self.run_package_operation(cmd, "remove")

    def reinstall_package(self):
        model, itr = self.tree.get_selection().get_selected()
        if itr is None:
            return
        # New column indices: icon(0), name(1), version(2), description(3), repository(4), installed(5)
        name = model[itr][1]  # name is now at index 1
        repo = model[itr][4]  # repository is now at index 4
        if not self.request_sudo():
            return
        if not self._confirm("Confirm Reinstallation", f"Are you sure you want to reinstall '{name}'?"):
            return
        if repo == 'aur':
            cmd = ["yay", "-S", name, "--noconfirm"]
        else:
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

    def run_package_operation(self, command, operation_type):
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

        self.current_worker = PackageOpWorker(command, operation_type, self.sudo_password, ui_callbacks())
        self.current_worker.start()

    def on_operation_finished(self, success, message):
        self.set_progress_value(0)
        self.set_status("Ready")
        # enable controls
        self._set_controls_sensitive(True)
        if success:
            self._info("Success", message)
            if self.search_type.get_active_text() == 'Installed':
                self.load_installed_packages()
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
                r = subprocess.run(["bash", "-lc", "pacman -Qu"], capture_output=True, text=True, timeout=30)
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
                self.upd_store.append([False, it['name'], f"{it['from']} -> {it['to']}"])
        # Update title with a badge-like count (simple)
        if hasattr(self, 'notebook'):
            idx = 3  # Updates tab index (Packages, Details, Repos, Updates)
            if idx < self.notebook.get_n_pages():
                self.notebook.set_tab_label_text(self.notebook.get_nth_page(idx), f"Updates ({len(self.updates_data)})")

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
                to_update.append(self.upd_store[it][1])
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
        # Autoremove orphaned packages
        cmd = ["bash", "-lc", "pacman -Qtdq | sudo -S pacman -Rns --noconfirm -"]
        self.run_package_operation(cmd, "remove orphans")

    def show_about(self):
        about_text = f"""
{APP_NAME} {APP_VERSION}

A Professional Package Manager for Arch Linux

PKGER is a comprehensive and modern package management application designed specifically for Arch Linux systems. It provides an intuitive graphical interface for managing packages, repositories, and system updates.

Features:
• Modern GTK-based user interface
• Search across official repositories and AUR
• Package installation, removal, and updates
• Repository management and synchronization
• Local package file installation
• System update and maintenance tools
• Enhanced sorting and filtering capabilities
• Professional package icons and visual indicators

Technical Details:
• Built with Python 3 and GTK 3
• Compatible with pacman package manager
• AUR support via yay helper
• Multi-threaded operations for better performance
• Responsive and user-friendly design

Developer Information:
• Developer: {APP_DEVELOPER}
• Version: {APP_VERSION}
• Year: {APP_YEAR}
• License: Open Source

This application aims to provide a professional alternative to existing package managers like pamac and octopi, with enhanced features and modern design principles.
        """
        
        about = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=f"{APP_NAME} {APP_VERSION}",
        )
        about.format_secondary_text(about_text.strip())
        about.run()
        about.destroy()

    # Core data functions
    def load_installed_packages(self):
        self.append_output("Loading installed packages...")
        self.set_status("Loading packages...")
        def worker():
            try:
                r = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=20)
                packages = []
                versions = {}
                if r.returncode == 0:
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

    def _on_category_filter_changed(self, button):
        """Handle category filter changes"""
        if not hasattr(self, 'packages_cache') or not self.packages_cache:
            return
        
        # Get active category
        active_category = None
        for cat, btn in self.category_buttons.items():
            if btn.get_active() and cat != 'All':
                active_category = cat
                break
        
        # Filter packages
        if active_category:
            filtered_packages = []
            for pkg in self.packages_cache:
                if hasattr(pkg, 'category'):
                    pkg_category = pkg.category
                else:
                    pkg_category = self.icon_manager._get_package_category(pkg.get('name', ''))
                
                if pkg_category.lower() == active_category.lower():
                    filtered_packages.append(pkg)
            
            self.update_package_list(filtered_packages)
        else:
            # Show all packages
            self.update_package_list(self.packages_cache)
    
    def search_packages(self):
        """Simple and working search function with AUR support"""
        query = self.search_entry.get_text().strip()
        if not query:
            return
        
        self.set_status("Searching...")
        self.append_output(f"Searching for '{query}'...")
        
        # Run search in background
        def search_worker():
            try:
                packages = []
                
                # Search official repositories
                self.append_output("Searching official repositories...")
                result = subprocess.run(["pacman", "-Ss", query], capture_output=True, text=True, timeout=10)
                
                if result.returncode == 0 and result.stdout.strip():
                    # Parse official results
                    lines = result.stdout.strip().split('\n')
                    current_package = None
                    
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        
                        # Package line (contains /)
                        if '/' in line and not line.startswith('    '):
                            parts = line.split(' ', 1)
                            if len(parts) >= 2:
                                repo_name = parts[0]
                                version_desc = parts[1]
                                
                                # Extract repo and name
                                if '/' in repo_name:
                                    repo, name = repo_name.split('/', 1)
                                else:
                                    repo = 'unknown'
                                    name = repo_name
                                
                                # Extract version
                                version = version_desc
                                installed = False
                                if '[installed:' in version_desc:
                                    version = version_desc.split(' [installed:', 1)[0]
                                    installed = True
                                elif '[installed]' in version_desc:
                                    version = version_desc.replace(' [installed]', '')
                                    installed = True
                                
                                current_package = {
                                    'name': name,
                                    'version': version,
                                    'description': '',
                                    'repository': repo,
                                    'installed': installed
                                }
                                packages.append(current_package)
                        
                        # Description line
                        elif line.startswith('    ') and current_package:
                            description = line.strip()
                            if description and not description.startswith('['):
                                current_package['description'] = description
                
                # Search AUR repositories
                self.append_output("Searching AUR...")
                try:
                    aur_result = subprocess.run(["yay", "-Ss", query], capture_output=True, text=True, timeout=15)
                    
                    if aur_result.returncode == 0 and aur_result.stdout.strip():
                        # Parse AUR results
                        lines = aur_result.stdout.strip().split('\n')
                        current_package = None
                        
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            
                            # AUR package line (contains aur/)
                            if 'aur/' in line and not line.startswith('    '):
                                parts = line.split(' ', 1)
                                if len(parts) >= 2:
                                    repo_name = parts[0]
                                    version_desc = parts[1]
                                    
                                    # Extract name (remove aur/ prefix)
                                    name = repo_name.replace('aur/', '')
                                    
                                    # Extract version (remove vote info and [installed])
                                    version = version_desc
                                    installed = False
                                    
                                    # Remove vote info like (+5 -0)
                                    if '(' in version and ')' in version:
                                        version = version.split('(', 1)[0].strip()
                                    
                                    # Check for [installed]
                                    if '[installed]' in version:
                                        version = version.replace(' [installed]', '')
                                        installed = True
                                    
                                    current_package = {
                                        'name': name,
                                        'version': version,
                                        'description': '',
                                        'repository': 'aur',
                                        'installed': installed
                                    }
                                    packages.append(current_package)
                            
                            # Description line
                            elif line.startswith('    ') and current_package:
                                description = line.strip()
                                if description and not description.startswith('['):
                                    current_package['description'] = description
                except Exception as e:
                    self.append_output(f"AUR search error: {e}")
                
                # Update UI with results
                GLib.idle_add(self._display_search_results, packages, query)
                
            except Exception as e:
                GLib.idle_add(self.append_output, f"Search error: {e}")
                GLib.idle_add(self.set_status, "Search failed")
        
        threading.Thread(target=search_worker, daemon=True).start()
    
    def _display_search_results(self, packages, query):
        """Display search results in the UI"""
        self.packages_cache = packages
        self.store.clear()
        
        # Add packages to the list
        for pkg in packages:
            name = pkg.get('name', '')
            version = pkg.get('version', '')
            description = pkg.get('description', '')
            repo = pkg.get('repository', '')
            installed = pkg.get('installed', False)
            
            # Get icon
            icon = self.icon_manager.get_package_icon(name, 20) if hasattr(self, 'icon_manager') else None
            
            # Add to store: icon, name, version, description, repository, installed
            self.store.append([icon, name, version, description, repo, installed])
        
        # Update counters
        self._update_package_counters()
        self._apply_package_styling()
        self.count_label.set_markup(f"<span size='small' foreground='#666666'>{len(packages)} packages</span>")
        
        # Show results
        self.append_output(f"Found {len(packages)} packages for '{query}'")
        self.set_status("Search completed")

    # ----------------------------
    # Repositories data loading and actions
    # ----------------------------
    def load_repos_data(self):
        if getattr(self, 'repos_worker', None) and self.repos_worker.is_alive():
            return
        def worker():
            data = {}
            try:
                # pacman -Sl: list repos and packages (installed marked with [installed])
                r = subprocess.run(["bash", "-lc", "pacman -Sl"], capture_output=True, text=True, timeout=60)
                for line in r.stdout.split('\n'):
                    if not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    repo = parts[0]
                    name = parts[1]
                    installed = '[installed]' in line
                    data.setdefault(repo, []).append({'name': name, 'installed': installed})
            except Exception:
                pass
            GLib.idle_add(self._apply_repos_data, data)
        self.repos_worker = threading.Thread(target=worker, daemon=True)
        self.repos_worker.start()

    def _apply_repos_data(self, data):
        self.repos_data = data or {}
        # Fill vertical repos list and right table
        if hasattr(self, 'repos_list'):
            self.repos_list.clear()
            for repo in sorted(self.repos_data.keys()):
                self.repos_list.append([repo])
        # Default select first repo
        if hasattr(self, 'repos_view'):
            sel = self.repos_view.get_selection()
            if self.repos_list and len(self.repos_list) > 0:
                itr = self.repos_list.get_iter_first()
                sel.select_iter(itr)
                self._populate_repo_packages(self.repos_list[itr][0])

    def _filter_repos_tree(self):
        # Refresh right table according to current repo and filters
        if not hasattr(self, 'repos_view'):
            return
        model, itr = self.repos_view.get_selection().get_selected()
        if itr is None:
            return
        repo = model[itr][0]
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
            self.repo_pkgs_store.append([False, it['name'], it['installed']])
            count += 1
            if limit is not None and count >= limit:
                break

    def _on_repo_selected(self, selection):
        model, itr = selection.get_selected()
        if itr is None:
            return
        repo = model[itr][0]
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
        repo = repo_model[repo_itr][0] if repo_model and repo_itr else 'unknown'
        name = model[itr][1]
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
        repo = model[itr][0] if itr else None
        if not repo:
            return pkgs
        it = self.repo_pkgs_store.get_iter_first()
        while it:
            if self.repo_pkgs_store[it][0]:
                name = self.repo_pkgs_store[it][1]
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
        self.install_btn.set_sensitive(enabled and self.install_btn.get_sensitive())
        self.remove_btn.set_sensitive(enabled and self.remove_btn.get_sensitive())
        self.reinstall_btn.set_sensitive(enabled and self.reinstall_btn.get_sensitive())
        self.update_btn.set_sensitive(enabled)
        self.clean_btn.set_sensitive(enabled)
        self.browse_btn.set_sensitive(enabled)

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

    def _render_repository_column(self, column, cell, model, itr, data=None):
        """Render repository column with color coding"""
        repo = model[itr][4]
        if repo == 'aur':
            cell.set_property('text', '⭐ AUR')
            cell.set_property('foreground', '#FF9800')
        elif repo == 'core':
            cell.set_property('text', '🔴 Core')
            cell.set_property('foreground', '#F44336')
        elif repo == 'extra':
            cell.set_property('text', '🔵 Extra')
            cell.set_property('foreground', '#2196F3')
        elif repo == 'community':
            cell.set_property('text', '🟢 Community')
            cell.set_property('foreground', '#4CAF50')
        elif repo == 'installed':
            cell.set_property('text', '💾 Installed')
            cell.set_property('foreground', '#9C27B0')
        else:
            cell.set_property('text', repo)
            cell.set_property('foreground', '#666666')

    def _render_installed_column(self, column, cell, model, itr, data=None):
        """Render installed column with status icons"""
        installed = model[itr][5]
        if installed:
            cell.set_property('text', '✅ Yes')
            cell.set_property('foreground', '#4CAF50')
        else:
            cell.set_property('text', '📦 No')
            cell.set_property('foreground', '#666666')

    def _enhance_package_display(self, package_name, icon):
        """Enhance package display with better icon handling"""
        if not icon:
            # Create a fallback icon based on package name
            icon = self.icon_manager.get_better_fallback_icon(package_name)
        
        # If still no icon, use the default package icon
        if not icon:
            icon = self.icon_manager._load_icon_by_name('package-x-generic', 20)
        
        # Scale icon to appropriate size if needed
        if icon and hasattr(icon, 'get_width'):
            current_width = icon.get_width()
            if current_width != 20:
                try:
                    icon = icon.scale_simple(20, 20, GdkPixbuf.InterpType.BILINEAR)
                except:
                    # If scaling fails, try to get a new icon at the right size
                    icon = self.icon_manager._load_icon_by_name('package-x-generic', 20)
        
        return icon
    
    def _update_package_counters(self):
        """Update package counters with enhanced styling"""
        if hasattr(self, 'packages_count_label'):
            total_packages = len(self.packages_cache) if hasattr(self, 'packages_cache') else 0
            self.packages_count_label.set_markup(f"<span size='medium'><b>📦 Packages:</b> {total_packages}</span>")
        
        if hasattr(self, 'updates_count_label'):
            total_updates = len(self.updates_data) if hasattr(self, 'updates_data') else 0
            self.updates_count_label.set_markup(f"<span size='medium'><b>🔄 Updates:</b> {total_updates}</span>")
        
        if hasattr(self, 'repos_count_label'):
            total_repos = len(self.repos_data) if hasattr(self, 'repos_data') else 0
            self.repos_count_label.set_markup(f"<span size='medium'><b>🏪 Repos:</b> {total_repos}</span>")
    
    def _apply_package_styling(self):
        """Apply enhanced styling to package list"""
        if hasattr(self, 'tree'):
            # Enable sorting
            self.tree.set_reorderable(True)
            
            # Set selection mode
            self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
            
            # Add hover effects
            self.tree.connect("motion-notify-event", self._on_package_hover)
            
            # Set alternating row colors using modern method
            try:
                # Try to use modern styling if available
                self.tree.set_alternating_row_colors(True)
            except:
                # Fallback for older GTK versions
                pass
    
    def _on_package_hover(self, widget, event):
        """Handle package list hover effects"""
        # This can be used to add tooltips or other hover effects
        pass

    def _advanced_search(self, query, filters=None):
        """Advanced search with multiple filters"""
        if not query and not filters:
            return self.packages_cache
        
        results = []
        query_lower = query.lower() if query else ""
        
        for pkg in self.packages_cache:
            # Check if package matches query
            if query:
                name_match = query_lower in pkg.name.lower() if hasattr(pkg, 'name') else query_lower in pkg.get('name', '').lower()
                desc_match = query_lower in pkg.description.lower() if hasattr(pkg, 'description') else query_lower in pkg.get('description', '').lower()
                if not (name_match or desc_match):
                    continue
            
            # Apply filters
            if filters:
                if not self._package_matches_filters(pkg, filters):
                    continue
            
            results.append(pkg)
        
        return results
    
    def _package_matches_filters(self, pkg, filters):
        """Check if package matches given filters"""
        for filter_type, filter_value in filters.items():
            if filter_type == 'repository':
                pkg_repo = pkg.repository if hasattr(pkg, 'repository') else pkg.get('repository', '')
                if filter_value != 'all' and pkg_repo != filter_value:
                    return False
            
            elif filter_type == 'installed':
                pkg_installed = pkg.installed if hasattr(pkg, 'installed') else pkg.get('installed', False)
                if filter_value != 'all' and pkg_installed != filter_value:
                    return False
            
            elif filter_type == 'category':
                pkg_category = pkg.category if hasattr(pkg, 'category') else self.icon_manager._get_package_category(pkg.get('name', ''))
                if filter_value != 'all' and pkg_category.lower() != filter_value.lower():
                    return False
        
        return True
    
    def _create_search_filters(self):
        """Create advanced search filters"""
        filters = {}
        
        # Repository filter
        search_type_text = self.search_type.get_active_text()
        if search_type_text == "📦 Official":
            filters['repository'] = 'official'
        elif search_type_text == "⭐ AUR":
            filters['repository'] = 'aur'
        elif search_type_text == "💾 Installed":
            filters['installed'] = True
        
        # Category filter
        active_category = None
        for cat, btn in self.category_buttons.items():
            if btn.get_active() and cat != 'All':
                active_category = cat
                break
        
        if active_category:
            filters['category'] = active_category
        
        return filters
    
    def _perform_advanced_search(self):
        """Perform advanced search with all filters"""
        query = self.search_entry.get_text().strip()
        filters = self._create_search_filters()
        
        if not query and not filters:
            # Show all packages
            self.update_package_list(self.packages_cache)
            return
        
        # Perform search
        results = self._advanced_search(query, filters)
        self.update_package_list(results)
        
        # Update status
        self.set_status(f"Found {len(results)} packages matching criteria")
        self.append_output(f"Advanced search completed: {len(results)} packages found")

    def _refresh_details(self, *args):
        """Refresh package details"""
        if hasattr(self, 'tree'):
            model, itr = self.tree.get_selection().get_selected()
            if itr is not None:
                name = model[itr][1]  # name is at index 1
                repo = model[itr][4]  # repository is at index 4
                self.fetch_and_show_details(name, repo)
    
    def _install_from_details(self, *args):
        """Install package from details page"""
        if hasattr(self, 'd_name'):
            name = self.d_name.get_text()
            if name and name != '-':
                self.install_package_by_name(name)
    
    def _remove_from_details(self, *args):
        """Remove package from details page"""
        if hasattr(self, 'd_name'):
            name = self.d_name.get_text()
            if name and name != '-':
                self.remove_package_by_name(name)
    
    def install_package_by_name(self, name):
        """Install package by name"""
        if not self.request_sudo():
            return
        
        # Try to determine repository
        repo = 'unknown'
        if hasattr(self, 'd_repo'):
            repo_text = self.d_repo.get_text()
            if ':' in repo_text:
                repo = repo_text.split(':', 1)[1].strip()
        
        if not self._confirm("Confirm Installation", f"Are you sure you want to install '{name}'?"):
            return
        
        if repo == 'aur':
            cmd = ["yay", "-S", name, "--noconfirm"]
        else:
            cmd = ["pacman", "-S", name, "--noconfirm"]
        
        self.run_package_operation(cmd, "install")
    
    def remove_package_by_name(self, name):
        """Remove package by name"""
        if not self.request_sudo():
            return
        
        if not self._confirm("Confirm Removal", f"Are you sure you want to remove '{name}'?"):
            return
        
        cmd = ["pacman", "-R", name, "--noconfirm"]
        self.run_package_operation(cmd, "remove")

    def _search_local_packages(self, query):
        """Search local package files in common directories"""
        packages = []
        try:
            self.append_output("Searching local package files...")
            
            # Common directories where local packages might be stored
            local_dirs = [
                os.path.expanduser("~/Downloads"),
                os.path.expanduser("~/Desktop"),
                os.path.expanduser("~/"),
                "/tmp",
                "/var/cache/pacman/pkg"
            ]
            
            for directory in local_dirs:
                if os.path.exists(directory):
                    try:
                        for filename in os.listdir(directory):
                            if filename.endswith(('.pkg.tar.zst', '.pkg.tar.xz')) and query.lower() in filename.lower():
                                # Extract package name from filename
                                # Format: name-version-arch.pkg.tar.zst
                                name = filename.split('-')[0]
                                version = '-'.join(filename.split('-')[1:-2]) if len(filename.split('-')) > 2 else 'unknown'
                                
                                package_info = {
                                    'name': name,
                                    'version': version,
                                    'description': f'Local package file: {filename}',
                                    'repository': 'local',
                                    'installed': False,
                                    'file_path': os.path.join(directory, filename)
                                }
                                packages.append(package_info)
                    except Exception as e:
                        self.append_output(f"Error scanning {directory}: {e}")
                        continue
        except Exception as e:
            self.append_output(f"Local packages search error: {e}")
        
        return packages

    # ----------------------------
    # Menu Helper Functions
    # ----------------------------
    
    def _export_package_list(self):
        """Export current package list to file"""
        dialog = Gtk.FileChooserDialog(title="Export Package List", parent=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("package-list.txt")
        resp = dialog.run()
        filename = dialog.get_filename() if resp == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not filename:
            return
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"PKGER Package List Export\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Total Packages: {len(self.packages_cache)}\n\n")
                for pkg in self.packages_cache:
                    if hasattr(pkg, 'name'):
                        f.write(f"{pkg.name} - {pkg.version} ({pkg.repository})\n")
                    else:
                        f.write(f"{pkg.get('name', '')} - {pkg.get('version', '')} ({pkg.get('repository', '')})\n")
            self._info("Exported", "Package list exported successfully.")
        except Exception as e:
            self._error("Export Failed", str(e))
    
    def _show_system_info(self):
        """Show system information dialog"""
        try:
            # Get system information
            arch = os.uname().machine if hasattr(os, 'uname') else 'unknown'
            user = os.getenv('USER', 'unknown')
            hostname = os.uname().nodename if hasattr(os, 'uname') else 'unknown'
            
            # Get package count
            try:
                result = subprocess.run(["pacman", "-Q"], capture_output=True, text=True, timeout=5)
                package_count = len(result.stdout.strip().split('\n')) if result.stdout.strip() else 0
            except:
                package_count = 0
            
            info_text = f"""
System Information:
• Hostname: {hostname}
• User: {user}
• Architecture: {arch}
• Installed Packages: {package_count}
• PKGER Version: {APP_VERSION}
• Developer: {APP_DEVELOPER}
• Year: {APP_YEAR}
            """
            
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="System Information"
            )
            dialog.format_secondary_text(info_text.strip())
            dialog.run()
            dialog.destroy()
        except Exception as e:
            self._error("Error", f"Could not retrieve system information: {e}")
    
    def _show_package_stats(self):
        """Show package statistics dialog"""
        try:
            # Count packages by repository
            repo_stats = {}
            installed_count = 0
            
            for pkg in self.packages_cache:
                if hasattr(pkg, 'repository'):
                    repo = pkg.repository
                    installed = pkg.installed
                else:
                    repo = pkg.get('repository', 'unknown')
                    installed = pkg.get('installed', False)
                
                repo_stats[repo] = repo_stats.get(repo, 0) + 1
                if installed:
                    installed_count += 1
            
            stats_text = f"""
Package Statistics:
• Total Packages: {len(self.packages_cache)}
• Installed Packages: {installed_count}
• Available Packages: {len(self.packages_cache) - installed_count}

By Repository:
"""
            for repo, count in sorted(repo_stats.items()):
                stats_text += f"• {repo}: {count}\n"
            
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Package Statistics"
            )
            dialog.format_secondary_text(stats_text.strip())
            dialog.run()
            dialog.destroy()
        except Exception as e:
            self._error("Error", f"Could not retrieve package statistics: {e}")
    
    def _show_user_guide(self):
        """Show user guide dialog"""
        guide_text = f"""
PKGER User Guide

Basic Operations:
• Search: Use the search bar to find packages
• Install: Select a package and click Install
• Remove: Select an installed package and click Remove
• Update: Use the Update button to update the system

Search Options:
• All: Search in all repositories
• Official: Search official repositories only
• AUR: Search AUR packages only
• Installed: Search installed packages only
• Local: Search local package files

Column Management:
• Resize: Drag column edges to resize
• Sort: Click column headers to sort
• Reorder: Drag column headers to reorder

Keyboard Shortcuts:
• Ctrl+F: Focus search bar
• Ctrl+R: Refresh package list
• Ctrl+Q: Quit application
• F1: Show this help

For more information, visit the project repository.
        """
        
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="PKGER User Guide"
        )
        dialog.format_secondary_text(guide_text.strip())
        dialog.run()
        dialog.destroy()
    
    def _show_keyboard_shortcuts(self):
        """Show keyboard shortcuts dialog"""
        shortcuts_text = f"""
Keyboard Shortcuts:

Navigation:
• Tab: Navigate between elements
• Arrow Keys: Navigate package list
• Enter: Activate selected item
• Space: Select/deselect items

Search & Filter:
• Ctrl+F: Focus search bar
• Ctrl+L: Clear search
• Ctrl+R: Refresh data
• Ctrl+A: Select all packages

Package Operations:
• Ctrl+I: Install selected package
• Ctrl+D: Remove selected package
• Ctrl+U: Update system
• Ctrl+C: Clean cache

Application:
• Ctrl+Q: Quit application
• F1: Show help
• F5: Refresh all data
• Ctrl+S: Export data

Note: Some shortcuts may vary depending on your system.
        """
        
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Keyboard Shortcuts"
        )
        dialog.format_secondary_text(shortcuts_text.strip())
        dialog.run()
        dialog.destroy()


def main():
    if not command_exists('pacman'):
        print("Warning: pacman not found in PATH. Some features will not work.")
    app = PKGERWindow()
    app.connect("destroy", Gtk.main_quit)
    app.show_all()
    Gtk.main()


if __name__ == '__main__':
    main()
