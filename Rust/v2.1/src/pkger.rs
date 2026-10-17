//! PKGER — single-file Rust package manager for Arch Linux (GTK4).
//! Build: cargo build --release

use gdk4 as gdk;
use gio::prelude::*;
use gtk4::prelude::*;
use gtk4::{
    Application, ApplicationWindow, Box as GtkBox, Button, CheckButton, CssProvider, Dialog,
    DropDown, Entry, FileChooserAction, FileChooserNative, FileFilter, Grid, Image, InputPurpose,
    Label, ListBox, ListBoxRow, Orientation, PolicyType, ProgressBar, ResponseType, ScrolledWindow,
    SelectionMode, Stack, StackTransitionType, StringList, TextView, WrapMode,
};
use pango::EllipsizeMode;
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write as IoWrite};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::rc::Rc;
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const APP_NAME: &str = "PKGER";
const APP_VERSION: &str = "2.1";
const NEWS_RSS: &str = "https://archlinux.org/feeds/news/";
const CACHE_TTL_SECS: u64 = 45;
const UI_ROW_LIMIT: usize = 180;
const SEARCH_RESULT_CAP: usize = 3000;
const LOAD_MORE_NAME: &str = "pkger-load-more";

static INSTALL_SOURCES: &[&str] = &[
    "Unified",
    "Official",
    "AUR",
    "Installed",
    "Flatpak",
    "AppImage",
    "Developer",
];
static INSTALL_SOURCE_KEYS: &[&str] = &[
    "unified",
    "official",
    "aur",
    "installed",
    "flatpak",
    "appimage",
    "developer",
];
static PACKAGE_SORTS: &[&str] = &["Name", "Repository", "Version", "Installed"];

// ---------------------------------------------------------------------------
// Paths / settings
// ---------------------------------------------------------------------------

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn config_dir() -> PathBuf {
    home_dir().join(".config").join("pkger")
}
fn config_file() -> PathBuf {
    config_dir().join("settings.json")
}
fn history_file() -> PathBuf {
    config_dir().join("history.json")
}
fn favorites_file() -> PathBuf {
    config_dir().join("favorites.json")
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
struct Settings {
    auto_check_updates: bool,
    show_security_alerts: bool,
    search_official: bool,
    search_aur: bool,
    search_flatpak: bool,
    search_appimage: bool,
    theme_accent: String,
    confirm_install: bool,
    confirm_remove: bool,
    max_search_results: i32,
    language: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    ignore_packages: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    ignore_groups: Vec<String>,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            auto_check_updates: true,
            show_security_alerts: true,
            search_official: true,
            search_aur: true,
            search_flatpak: true,
            search_appimage: true,
            theme_accent: "default".into(),
            confirm_install: true,
            confirm_remove: true,
            max_search_results: 80,
            language: "auto".into(),
            ignore_packages: vec![],
            ignore_groups: vec![],
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct Package {
    name: String,
    pkg: String,
    version: String,
    description: String,
    repository: String,
    installed: bool,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    path: String,
    #[serde(skip)]
    score: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct Update {
    name: String,
    from: String,
    to: String,
    #[serde(default)]
    repository: String,
    source: String,
    selected: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
struct RepoPackage {
    name: String,
    version: String,
    installed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct HistoryEntry {
    time: String,
    label: String,
    cmd: String,
}

#[derive(Debug, Clone, Default)]
struct NewsItem {
    title: String,
    link: String,
    summary: String,
    date: String,
}

#[derive(Debug, Clone, Default)]
struct SystemStats {
    kernel: String,
    uptime: String,
    load: String,
    disk_free: String,
    mem_used_pct: String,
    cache_size: i64,
}

struct CachedSearch {
    at: Instant,
    results: Vec<Package>,
}

struct App {
    settings: Settings,
    history: Vec<HistoryEntry>,
    favorites: HashMap<String, bool>,
    packages: Vec<Package>,
    updates: Vec<Update>,
    repos: HashMap<String, Vec<RepoPackage>>,
    repos_loaded: HashSet<String>,
    app_images: Vec<Package>,
    stats: SystemStats,
    held: Vec<String>,
    log_lines: Vec<String>,
    last_results: Vec<Package>,
}

impl App {
    fn new() -> Self {
        let mut settings = Settings::default();
        let _ = read_json(&config_file(), &mut settings);
        let mut history: Vec<HistoryEntry> = vec![];
        let _ = read_json(&history_file(), &mut history);
        let mut favs: Vec<String> = vec![];
        let _ = read_json(&favorites_file(), &mut favs);
        let mut favorites = HashMap::new();
        for f in favs {
            favorites.insert(f, true);
        }
        Self {
            settings,
            history,
            favorites,
            packages: vec![],
            updates: vec![],
            repos: HashMap::new(),
            repos_loaded: HashSet::new(),
            app_images: vec![],
            stats: SystemStats::default(),
            held: vec![],
            log_lines: vec![],
            last_results: vec![],
        }
    }

    fn append_log(&mut self, s: &str) {
        let stamp = now_hms();
        self.log_lines.push(format!("[{stamp}] {s}"));
        if self.log_lines.len() > 1000 {
            let n = self.log_lines.len();
            self.log_lines = self.log_lines.split_off(n - 1000);
        }
    }

    fn append_history(&mut self, label: &str, cmd: &str) {
        self.history.push(HistoryEntry {
            time: now_datetime(),
            label: label.into(),
            cmd: cmd.into(),
        });
        if self.history.len() > 200 {
            let n = self.history.len();
            self.history = self.history.split_off(n - 200);
        }
        let _ = fs::create_dir_all(config_dir());
        let _ = write_json(&history_file(), &self.history);
    }

    fn save_settings(&mut self) {
        let _ = fs::create_dir_all(config_dir());
        if write_json(&config_file(), &self.settings).is_ok() {
            self.append_log("Settings saved");
        }
    }

    fn export_log(&mut self, path: Option<&str>) {
        let default = format!("pkger-log-{}.txt", now_compact());
        let path = path.unwrap_or(&default);
        let text = self.log_lines.join("\n") + "\n";
        if fs::write(path, text).is_ok() {
            self.append_log(&format!("Log exported to {path}"));
        }
    }

    fn export_repo(&mut self, selected_repo: &str) {
        if selected_repo.is_empty() {
            return;
        }
        let path = format!("pkger-{selected_repo}-packages.txt");
        let mut b = String::new();
        if let Some(pkgs) = self.repos.get(selected_repo) {
            for p in pkgs {
                b.push_str(&p.name);
                b.push('\n');
            }
        }
        if fs::write(&path, b).is_ok() {
            self.append_log(&format!("Exported repository list to {path}"));
        }
    }

    fn export_command(&mut self, label: &str, args: &[&str]) {
        let (out, err_out, code) = run_capture(Duration::from_secs(180), args);
        let mut out = out;
        if !err_out.is_empty() {
            out.push_str(&err_out);
        }
        let path = if label.to_lowercase().contains("qne") {
            "native-explicit-packages.txt"
        } else {
            "all-packages.txt"
        };
        if code != 0 {
            self.append_log(&format!("{label} failed: {}", out.trim()));
            return;
        }
        match fs::write(path, out) {
            Ok(()) => self.append_log(&format!("Exported package list to {path}")),
            Err(e) => self.append_log(&format!("Export failed: {e}")),
        }
    }

    fn load_pacman_log(&mut self) {
        match fs::read_to_string("/var/log/pacman.log") {
            Ok(b) => {
                let mut lines: Vec<String> = b.lines().map(|s| s.to_string()).collect();
                if lines.len() > 500 {
                    lines = lines.split_off(lines.len() - 500);
                }
                self.log_lines = lines;
            }
            Err(e) => self.append_log(&format!("Cannot read /var/log/pacman.log: {e}")),
        }
    }

    fn load_installed(&mut self) {
        let (out, _, code) = run_capture(Duration::from_secs(60), &["pacman", "-Q"]);
        if code != 0 {
            self.append_log("pacman -Q unavailable or failed");
            return;
        }
        let mut list = vec![];
        for line in out.lines() {
            let p: Vec<&str> = line.split_whitespace().collect();
            if p.len() >= 2 {
                list.push(Package {
                    name: p[0].into(),
                    pkg: p[0].into(),
                    version: p[1].into(),
                    repository: "installed".into(),
                    description: "Installed package".into(),
                    installed: true,
                    ..Default::default()
                });
            }
        }
        let n = list.len();
        self.packages = list.clone();
        self.last_results = list;
        self.append_log(&format!("Loaded {n} installed packages"));
    }

    fn load_repos(&mut self) {
        let names = list_repo_names();
        if names.is_empty() {
            self.append_log("No repositories found (pacman-conf / pacman.conf)");
            return;
        }
        let mut m: HashMap<String, Vec<RepoPackage>> = HashMap::new();
        for name in names {
            m.entry(name).or_default();
        }
        let n = m.len();
        self.repos = m;
        self.repos_loaded.clear();
        self.append_log(&format!("Indexed {n} repositories"));
    }

    fn load_repo_packages(&mut self, repo: &str) {
        if repo.is_empty() || self.repos_loaded.contains(repo) {
            return;
        }
        let (sl, err, code) = run_capture(Duration::from_secs(45), &["pacman", "-Sl", repo]);
        let part = parse_pacman_sl(&sl);
        let pkgs = part
            .get(repo)
            .cloned()
            .or_else(|| {
                if part.len() == 1 {
                    part.into_iter().next().map(|(_, v)| v)
                } else {
                    None
                }
            })
            .unwrap_or_default();
        if pkgs.is_empty() && code != 0 {
            self.append_log(&format!(
                "Failed to load {repo}: {}",
                if err.trim().is_empty() {
                    format!("exit {code}")
                } else {
                    err.trim().to_string()
                }
            ));
        }
        let n = pkgs.len();
        self.repos.insert(repo.to_string(), pkgs);
        self.repos_loaded.insert(repo.to_string());
        self.append_log(&format!("Loaded {n} packages from {repo}"));
    }

    fn load_updates(&mut self, sync_db: bool) {
        if sync_db {
            // sync via tool mapping in GUI; here just call pacman -Sy if needed
            let _ = run_capture(
                Duration::from_secs(120),
                &["sudo", "-n", "pacman", "-Sy", "--noconfirm"],
            );
        }
        let (out, _, code) = run_capture(Duration::from_secs(55), &["pacman", "-Qu"]);
        let mut list = vec![];
        if code == 0 {
            for line in out.lines() {
                let p: Vec<&str> = line.split_whitespace().collect();
                if p.len() >= 4 && p[2] == "->" {
                    list.push(Update {
                        name: p[0].into(),
                        from: p[1].into(),
                        to: p[3].into(),
                        source: "pacman".into(),
                        ..Default::default()
                    });
                }
            }
        }
        if command_exists("flatpak") {
            let (f, _, c) = run_capture(
                Duration::from_secs(45),
                &[
                    "flatpak",
                    "remote-ls",
                    "--updates",
                    "--columns=application,version",
                    "flathub",
                ],
            );
            if c == 0 {
                for line in f.lines() {
                    let p: Vec<&str> = line.split_whitespace().collect();
                    if !p.is_empty() {
                        let v = if p.len() > 1 { p[1] } else { "update" };
                        list.push(Update {
                            name: p[0].into(),
                            from: "-".into(),
                            to: v.into(),
                            source: "flatpak".into(),
                            ..Default::default()
                        });
                    }
                }
            }
        }
        let n = list.len();
        self.updates = list;
        self.append_log(&format!("Loaded {n} updates"));
    }

    fn scan_app_images(&mut self) {
        self.app_images = scan_app_images("");
        let n = self.app_images.len();
        self.append_log(&format!("Found {n} AppImages"));
    }

    fn installed_set(&self) -> HashSet<String> {
        self.packages.iter().map(|p| p.name.clone()).collect()
    }

    fn search(&mut self, text: &str) {
        let mut query = text.trim().to_string();
        let mut mode = "unified".to_string();
        let fields: Vec<&str> = query.split_whitespace().collect();
        if fields.len() > 1 {
            let candidate = fields[fields.len() - 1].to_lowercase();
            if is_search_mode(&candidate) {
                mode = candidate;
                query = fields[..fields.len() - 1].join(" ");
            }
        }
        if query == "installed" {
            mode = "installed".into();
            query.clear();
        }
        if query.is_empty() && mode != "installed" && mode != "appimage" {
            return;
        }

        static SEARCH_CACHE: Mutex<Option<HashMap<String, CachedSearch>>> = Mutex::new(None);
        let key = format!("{}|{}", mode, query.to_lowercase());
        {
            let mut guard = SEARCH_CACHE.lock().unwrap();
            if guard.is_none() {
                *guard = Some(HashMap::new());
            }
            if let Some(cache) = guard.as_ref() {
                if let Some(c) = cache.get(&key) {
                    if c.at.elapsed() < Duration::from_secs(CACHE_TTL_SECS) {
                        self.last_results = c.results.clone();
                        return;
                    }
                }
            }
        }

        let installed = self.installed_set();
        let mut handles = vec![];
        let q = query.clone();
        let mode_c = mode.clone();

        if mode_c == "official" || mode_c == "unified" {
            let q2 = q.clone();
            let inst = installed.clone();
            handles.push(thread::spawn(move || search_pacman(&q2, &inst)));
        }
        if mode_c == "installed" {
            let q2 = q.clone();
            let inst = installed.clone();
            handles.push(thread::spawn(move || search_installed(&q2, &inst)));
        }
        if mode_c == "aur" || mode_c == "unified" {
            let q2 = q.clone();
            let inst = installed.clone();
            handles.push(thread::spawn(move || search_aur(&q2, &inst)));
        }
        if mode_c == "flatpak" || mode_c == "unified" {
            let q2 = q.clone();
            handles.push(thread::spawn(move || search_flatpak(&q2)));
        }
        if mode_c == "appimage" || mode_c == "unified" {
            let q2 = q.clone();
            handles.push(thread::spawn(move || scan_app_images(&q2)));
        }
        if mode_c == "developer" {
            let q2 = q.clone();
            let inst = installed.clone();
            handles.push(thread::spawn(move || search_pacman(&q2, &inst)));
        }

        let mut merged = vec![];
        let mut seen = HashSet::new();
        for h in handles {
            if let Ok(batch) = h.join() {
                for p in batch {
                    let k = format!("{}/{}", p.repository, p.pkg);
                    if seen.insert(k) {
                        merged.push(p);
                    }
                }
            }
        }

        let ql = query.to_lowercase();
        if !ql.is_empty() {
            let mut scored = vec![];
            for mut p in merged {
                let hay = format!("{} {} {}", p.name, p.description, p.repository).to_lowercase();
                let mut score = 0;
                if hay.contains(&ql) {
                    score += 50;
                }
                if fuzzy(&ql, &p.name.to_lowercase()) {
                    score += 25;
                }
                for t in ql.split_whitespace() {
                    if p.name.to_lowercase().contains(t) {
                        score += 12;
                    } else if p.description.to_lowercase().contains(t) {
                        score += 8;
                    }
                }
                if score == 0 {
                    continue;
                }
                if p.installed {
                    score += 1;
                }
                p.score = score;
                scored.push(p);
            }
            merged = scored;
        }
        merged.sort_by(|a, b| {
            b.score
                .cmp(&a.score)
                .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
        });
        for p in &mut merged {
            p.score = 0;
        }

        {
            let mut guard = SEARCH_CACHE.lock().unwrap();
            if let Some(cache) = guard.as_mut() {
                cache.insert(
                    key,
                    CachedSearch {
                        at: Instant::now(),
                        results: merged.clone(),
                    },
                );
            }
        }
        if merged.len() > SEARCH_RESULT_CAP {
            merged.truncate(SEARCH_RESULT_CAP);
        }
        let n = merged.len();
        self.last_results = merged;
        self.append_log(&format!("Search {query:?} in {mode} returned {n} packages"));
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path, v: &mut T) -> Result<(), String> {
    let b = fs::read(path).map_err(|e| e.to_string())?;
    *v = serde_json::from_slice(&b).map_err(|e| e.to_string())?;
    Ok(())
}

fn write_json<T: Serialize>(path: &Path, v: &T) -> Result<(), String> {
    let b = serde_json::to_vec_pretty(v).map_err(|e| e.to_string())?;
    fs::write(path, b).map_err(|e| e.to_string())
}

fn command_exists(name: &str) -> bool {
    Command::new("which")
        .arg(name)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn run_capture(timeout: Duration, args: &[&str]) -> (String, String, i32) {
    if args.is_empty() {
        return (String::new(), "no command".into(), 1);
    }
    let mut child = match Command::new(args[0])
        .args(&args[1..])
        .env("LANG", "C")
        .env("LC_ALL", "C")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => return (String::new(), e.to_string(), 1),
    };
    let mut stdout = match child.stdout.take() {
        Some(s) => s,
        None => return (String::new(), "missing stdout".into(), 1),
    };
    let mut stderr = match child.stderr.take() {
        Some(s) => s,
        None => return (String::new(), "missing stderr".into(), 1),
    };
    let out_h = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stdout.read_to_end(&mut buf);
        buf
    });
    let err_h = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stderr.read_to_end(&mut buf);
        buf
    });
    let start = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {
                if start.elapsed() > timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    let out = String::from_utf8_lossy(&out_h.join().unwrap_or_default()).into_owned();
                    let err = String::from_utf8_lossy(&err_h.join().unwrap_or_default()).into_owned();
                    let err = if err.trim().is_empty() {
                        "timeout".into()
                    } else {
                        format!("timeout: {}", err.trim())
                    };
                    return (out, err, 1);
                }
                thread::sleep(Duration::from_millis(20));
            }
            Err(e) => {
                let _ = out_h.join();
                let _ = err_h.join();
                return (String::new(), e.to_string(), 1);
            }
        }
    };
    let out = String::from_utf8_lossy(&out_h.join().unwrap_or_default()).into_owned();
    let err = String::from_utf8_lossy(&err_h.join().unwrap_or_default()).into_owned();
    (out, err, status.code().unwrap_or(1))
}

fn list_repo_names() -> Vec<String> {
    let (out, _, _) = run_capture(Duration::from_secs(5), &["pacman-conf", "--repo-list"]);
    let mut names: Vec<String> = out
        .lines()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
        .collect();
    if names.is_empty() {
        if let Ok(data) = fs::read_to_string("/etc/pacman.conf") {
            for line in data.lines() {
                let t = line.trim();
                if t.starts_with('[') && t.ends_with(']') && t.len() > 2 {
                    let name = &t[1..t.len() - 1];
                    if name != "options" && !name.starts_with('.') {
                        names.push(name.to_string());
                    }
                }
            }
        }
    }
    names.sort();
    names.dedup();
    names
}

fn parse_pacman_sl(out: &str) -> HashMap<String, Vec<RepoPackage>> {
    let mut m: HashMap<String, Vec<RepoPackage>> = HashMap::new();
    for line in out.lines() {
        let p: Vec<&str> = line.split_whitespace().collect();
        if p.len() >= 2 {
            let v = if p.len() > 2 { p[2] } else { "-" };
            m.entry(p[0].into()).or_default().push(RepoPackage {
                name: p[1].into(),
                version: v.into(),
                installed: line.contains("[installed]"),
            });
        }
    }
    m
}

fn priv_tool() -> String {
    if command_exists("sudo") {
        "sudo".into()
    } else if command_exists("doas") {
        "doas".into()
    } else {
        String::new()
    }
}

fn aur_helper() -> String {
    if command_exists("yay") {
        "yay".into()
    } else if command_exists("paru") {
        "paru".into()
    } else {
        String::new()
    }
}

fn is_search_mode(s: &str) -> bool {
    matches!(
        s,
        "unified" | "official" | "aur" | "installed" | "flatpak" | "appimage" | "developer"
    )
}

fn fuzzy(pattern: &str, text: &str) -> bool {
    if pattern.is_empty() {
        return true;
    }
    let mut chars = pattern.chars();
    let mut cur = chars.next();
    for r in text.chars() {
        if let Some(c) = cur {
            if c == r {
                cur = chars.next();
            }
        }
    }
    cur.is_none()
}

fn search_pacman(q: &str, installed: &HashSet<String>) -> Vec<Package> {
    if q.is_empty() {
        return vec![];
    }
    let (out, _, code) = run_capture(Duration::from_secs(40), &["pacman", "-Ss", q]);
    if code != 0 {
        return vec![];
    }
    parse_pacman(&out, "official", installed)
}

fn search_installed(q: &str, installed: &HashSet<String>) -> Vec<Package> {
    let args: Vec<&str> = if q.is_empty() {
        vec!["pacman", "-Q"]
    } else {
        vec!["pacman", "-Qs", q]
    };
    let (out, _, code) = run_capture(Duration::from_secs(40), &args);
    if code != 0 {
        return vec![];
    }
    parse_pacman(&out, "installed", installed)
}

fn search_aur(q: &str, installed: &HashSet<String>) -> Vec<Package> {
    let h = aur_helper();
    if h.is_empty() || q.is_empty() {
        return vec![];
    }
    let (out, _, code) = run_capture(Duration::from_secs(50), &[&h, "-Ss", q]);
    if code != 0 {
        return vec![];
    }
    parse_aur(&out, installed)
}

fn search_flatpak(q: &str) -> Vec<Package> {
    if !command_exists("flatpak") || q.is_empty() {
        return vec![];
    }
    let (out, _, code) = run_capture(
        Duration::from_secs(50),
        &[
            "flatpak",
            "search",
            "--columns=name,application,description",
            q,
        ],
    );
    if code != 0 {
        return vec![];
    }
    let mut r = vec![];
    for line in out.lines() {
        let p: Vec<&str> = line.split('\t').collect();
        if !p.is_empty() && !p[0].trim().is_empty() {
            let mut id = p[0].trim().to_string();
            if p.len() > 1 {
                id = p[1].trim().to_string();
            }
            let d = if p.len() > 2 {
                p[2].trim().to_string()
            } else {
                "Flatpak application".into()
            };
            r.push(Package {
                name: id.clone(),
                pkg: id,
                description: d,
                repository: "flatpak".into(),
                ..Default::default()
            });
        }
    }
    r
}

fn parse_pacman(out: &str, source: &str, installed: &HashSet<String>) -> Vec<Package> {
    let lines: Vec<&str> = out.lines().collect();
    let mut r = vec![];
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i].trim();
        if line.is_empty() || lines[i].starts_with(' ') {
            i += 1;
            continue;
        }
        let mut parts = line.splitn(2, ' ');
        let full = parts.next().unwrap_or("");
        let ver = parts.next().unwrap_or("").to_string();
        if full.is_empty() {
            i += 1;
            continue;
        }
        let (repo, name) = if let Some(j) = full.find('/') {
            (full[..j].to_string(), full[j + 1..].to_string())
        } else {
            (source.to_string(), full.to_string())
        };
        let mut desc = String::new();
        if i + 1 < lines.len() && lines[i + 1].starts_with("    ") {
            desc = lines[i + 1].trim().to_string();
            i += 1;
        }
        let inst = installed.contains(&name);
        r.push(Package {
            name: name.clone(),
            pkg: name,
            version: ver,
            description: desc,
            repository: repo,
            installed: inst,
            ..Default::default()
        });
        i += 1;
    }
    r
}

fn parse_aur(out: &str, installed: &HashSet<String>) -> Vec<Package> {
    let mut r = vec![];
    for line in out.lines() {
        if line.is_empty() || line.starts_with(' ') {
            continue;
        }
        let p: Vec<&str> = line.splitn(3, ' ').collect();
        if p.len() >= 2 {
            let mut name = p[0].to_string();
            if let Some(rest) = name.strip_prefix("aur/") {
                name = rest.to_string();
            }
            let ver = p[1].to_string();
            let d = if p.len() > 2 { p[2].to_string() } else { String::new() };
            let inst = installed.contains(&name);
            r.push(Package {
                name: name.clone(),
                pkg: name,
                version: ver,
                description: d,
                repository: "aur".into(),
                installed: inst,
                ..Default::default()
            });
        }
    }
    r
}

fn scan_app_images(query: &str) -> Vec<Package> {
    let home = home_dir();
    let locations = [
        home.join("Applications"),
        home.join(".local").join("bin"),
        PathBuf::from("/opt"),
    ];
    let q = query.to_lowercase();
    let mut out = vec![];
    for loc in &locations {
        if !loc.exists() {
            continue;
        }
        let depth = if loc.as_os_str() == "/opt" { 2 } else { 5 };
        let walker = walkdir_limited(loc, depth, 400);
        for path in walker {
            let ext = path
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or("")
                .to_lowercase();
            if ext == "appimage" {
                let fname = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("AppImage")
                    .to_string();
                let name = fname
                    .trim_end_matches(".AppImage")
                    .trim_end_matches(".appimage")
                    .to_string();
                if q.is_empty() || name.to_lowercase().contains(&q) {
                    let pstr = path.display().to_string();
                    out.push(Package {
                        name: name.clone(),
                        pkg: name,
                        description: format!("Local AppImage: {pstr}"),
                        repository: "appimage".into(),
                        installed: true,
                        path: pstr,
                        ..Default::default()
                    });
                }
            }
        }
    }
    out
}

fn walkdir_simple(root: &Path) -> Vec<PathBuf> {
    walkdir_limited(root, 4, 600)
}

fn walkdir_limited(root: &Path, max_depth: usize, max_files: usize) -> Vec<PathBuf> {
    let mut out = vec![];
    let mut stack = vec![(root.to_path_buf(), 0usize)];
    while let Some((dir, depth)) = stack.pop() {
        let Ok(rd) = fs::read_dir(&dir) else {
            continue;
        };
        for entry in rd.flatten() {
            let p = entry.path();
            if p.is_dir() {
                if depth >= max_depth {
                    continue;
                }
                let skip = p
                    .file_name()
                    .and_then(|n| n.to_str())
                    .map(|n| {
                        n.starts_with('.')
                            || matches!(
                                n,
                                "proc"
                                    | "sys"
                                    | "dev"
                                    | "node_modules"
                                    | "containerd"
                                    | "docker"
                                    | "libvirt"
                                    | "wine"
                                    | ".git"
                            )
                    })
                    .unwrap_or(false);
                if skip {
                    continue;
                }
                stack.push((p, depth + 1));
            } else {
                out.push(p);
                if out.len() >= max_files {
                    return out;
                }
            }
        }
    }
    out
}

fn detect_distro() -> String {
    let Ok(b) = fs::read_to_string("/etc/os-release") else {
        return "Arch Linux".into();
    };
    let mut name = "Arch Linux".to_string();
    let mut version = String::new();
    for line in b.lines() {
        if let Some((k, v)) = line.split_once('=') {
            let v = v.trim_matches('"');
            if k == "NAME" {
                name = v.to_string();
            }
            if k == "VERSION_ID" {
                version = v.to_string();
            }
        }
    }
    if version.is_empty() {
        name
    } else {
        format!("{name} {version}")
    }
}

fn severity(name: &str, repo: &str, source: &str) -> &'static str {
    let n = name.to_lowercase();
    for x in ["linux", "kernel"] {
        if n.contains(x) {
            return "Kernel";
        }
    }
    for x in ["openssl", "openssh", "sudo", "polkit", "glibc", "systemd"] {
        if n.contains(x) {
            return "Security";
        }
    }
    if source.eq_ignore_ascii_case("flatpak") || repo.eq_ignore_ascii_case("flatpak") {
        return "Important";
    }
    "Update"
}

fn health_score(_installed: usize, updates: &[Update], held: &[String], st: &SystemStats) -> i32 {
    let mut score = 100i32;
    let mut sec = 0;
    let mut kernel = 0;
    for u in updates {
        match severity(&u.name, &u.repository, &u.source) {
            "Security" => sec += 1,
            "Kernel" => kernel += 1,
            _ => {}
        }
    }
    if sec > 0 {
        score -= (sec * 8).min(30);
    }
    if kernel > 0 {
        score -= (kernel * 5).min(15);
    }
    if updates.len() > 50 {
        score -= 10;
    } else if updates.len() > 20 {
        score -= 5;
    }
    score -= (held.len() as i32 * 2).min(10);
    let gb = st.cache_size as f64 / (1024.0 * 1024.0 * 1024.0);
    if gb > 2.0 {
        score -= 10;
    } else if gb > 1.0 {
        score -= 5;
    }
    score.clamp(0, 100)
}

fn format_update_summary(u: &[Update]) -> String {
    let mut sec = 0;
    let mut fp = 0;
    for x in u {
        if severity(&x.name, &x.repository, &x.source) == "Security" {
            sec += 1;
        }
        if x.source == "flatpak" {
            fp += 1;
        }
    }
    format!(
        "Pending: {} total | pacman: {} | Flatpak: {} | Security-related: {}",
        u.len(),
        u.len() - fp,
        fp,
        sec
    )
}

fn classify_repo(r: &str) -> &'static str {
    let l = r.to_lowercase();
    if matches!(l.as_str(), "core" | "extra" | "multilib" | "community") {
        "official"
    } else if l.contains("testing") {
        "testing"
    } else if l.contains("chaotic") || l.contains("blackarch") || l.contains("archlinuxcn") {
        "third-party"
    } else {
        "community"
    }
}

fn human_size(n: i64) -> String {
    let units = ["B", "KB", "MB", "GB", "TB"];
    let mut f = n as f64;
    for u in units {
        if f < 1024.0 {
            return format!("{f:.1} {u}");
        }
        f /= 1024.0;
    }
    format!("{f:.1} PB")
}

fn truncate(s: &str, n: usize) -> String {
    let r: Vec<char> = s.chars().collect();
    if r.len() <= n {
        s.to_string()
    } else {
        let end = n.saturating_sub(1);
        format!("{}…", r[..end].iter().collect::<String>())
    }
}

fn dir_size(root: &Path) -> i64 {
    let path = root.display().to_string();
    let (o, _, c) = run_capture(Duration::from_secs(8), &["du", "-sb", &path]);
    if c == 0 {
        if let Some(n) = o.split_whitespace().next() {
            if let Ok(v) = n.parse::<i64>() {
                return v;
            }
        }
    }
    0
}

fn system_stats() -> SystemStats {
    let mut st = SystemStats {
        kernel: "-".into(),
        uptime: "-".into(),
        load: "-".into(),
        disk_free: "-".into(),
        mem_used_pct: "-".into(),
        cache_size: 0,
    };
    let (o, _, c) = run_capture(Duration::from_secs(5), &["uname", "-r"]);
    if c == 0 {
        st.kernel = o.trim().to_string();
    }
    if let Ok(b) = fs::read_to_string("/proc/uptime") {
        if let Some(first) = b.split_whitespace().next() {
            if let Ok(f) = first.parse::<f64>() {
                let mut mins = (f as i64) / 60;
                let days = mins / 1440;
                mins %= 1440;
                let hrs = mins / 60;
                mins %= 60;
                st.uptime = if days > 0 {
                    format!("{days}d {hrs}h {mins}m")
                } else {
                    format!("{hrs}h {mins}m")
                };
            }
        }
    }
    if let Ok(b) = fs::read_to_string("/proc/loadavg") {
        let parts: Vec<&str> = b.split_whitespace().collect();
        st.load = parts.iter().take(3).cloned().collect::<Vec<_>>().join(" ");
    }
    let (o, _, c) = run_capture(Duration::from_secs(10), &["df", "-h", "/"]);
    if c == 0 {
        let ls: Vec<&str> = o.trim().lines().collect();
        if ls.len() > 1 {
            let p: Vec<&str> = ls[1].split_whitespace().collect();
            if p.len() > 3 {
                st.disk_free = p[3].to_string();
            }
        }
    }
    if let Ok(b) = fs::read_to_string("/proc/meminfo") {
        let mut total = 0i64;
        let mut avail = 0i64;
        for line in b.lines() {
            let p: Vec<&str> = line.split_whitespace().collect();
            if p.len() >= 2 {
                if p[0] == "MemTotal:" {
                    total = p[1].parse().unwrap_or(0);
                }
                if p[0] == "MemAvailable:" {
                    avail = p[1].parse().unwrap_or(0);
                }
            }
        }
        if total > 0 {
            st.mem_used_pct = format!("{}%", 100 - avail * 100 / total);
        }
    }
    st.cache_size = dir_size(Path::new("/var/cache/pacman/pkg"));
    st
}

fn fetch_news(limit: usize) -> Vec<NewsItem> {
    let (out, _, code) = run_capture(
        Duration::from_secs(30),
        &[
            "curl",
            "-fsSL",
            "-A",
            &format!("{APP_NAME}/{APP_VERSION}"),
            NEWS_RSS,
        ],
    );
    let xml = if code == 0 && !out.is_empty() {
        out
    } else {
        // fallback: try wget
        let (o2, _, c2) = run_capture(
            Duration::from_secs(30),
            &["wget", "-qO-", NEWS_RSS],
        );
        if c2 == 0 {
            o2
        } else {
            return vec![];
        }
    };
    let mut result = vec![];
    for block in xml.split("<item>").skip(1) {
        if result.len() >= limit {
            break;
        }
        let get = |tag: &str| -> String {
            let start = format!("<{tag}>");
            let end = format!("</{tag}>");
            if let Some(i) = block.find(&start) {
                let i = i + start.len();
                if let Some(j) = block[i..].find(&end) {
                    return decode_xml(&block[i..i + j]);
                }
            }
            String::new()
        };
        result.push(NewsItem {
            title: get("title"),
            link: get("link"),
            summary: get("description"),
            date: get("pubDate"),
        });
    }
    result
}

fn decode_xml(s: &str) -> String {
    s.trim()
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
}

fn strip_html(s: &str) -> String {
    let mut b = String::new();
    let mut inside = false;
    for r in s.chars() {
        if r == '<' {
            inside = true;
            continue;
        }
        if r == '>' {
            inside = false;
            continue;
        }
        if !inside {
            b.push(r);
        }
    }
    b.trim().to_string()
}

fn yes_no(v: bool) -> &'static str {
    if v {
        "Installed"
    } else {
        "Not installed"
    }
}

fn package_status(p: &Package) -> String {
    if p.installed {
        return "Installed".into();
    }
    if p.repository.eq_ignore_ascii_case("aur") {
        return "AUR package".into();
    }
    if p.repository.eq_ignore_ascii_case("flatpak") {
        return "Flatpak application".into();
    }
    "Available to install".into()
}

fn now_hms() -> String {
    use std::time::Duration as D;
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(D::ZERO)
        .as_secs();
    // local time approximation via date command for simplicity / accuracy
    let (o, _, c) = run_capture(Duration::from_secs(2), &["date", "+%H:%M:%S"]);
    if c == 0 && !o.trim().is_empty() {
        o.trim().to_string()
    } else {
        format!("{:02}:{:02}:{:02}", (secs / 3600) % 24, (secs / 60) % 60, secs % 60)
    }
}

fn now_datetime() -> String {
    let (o, _, c) = run_capture(Duration::from_secs(2), &["date", "+%Y-%m-%d %H:%M:%S"]);
    if c == 0 {
        o.trim().to_string()
    } else {
        now_hms()
    }
}

fn now_compact() -> String {
    let (o, _, c) = run_capture(Duration::from_secs(2), &["date", "+%Y%m%d-%H%M%S"]);
    if c == 0 {
        o.trim().to_string()
    } else {
        "export".into()
    }
}

fn split_policy(text: &str) -> Vec<String> {
    text.split(|c: char| c == ',' || c == ' ' || c == '\n' || c == '\t')
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .collect()
}

fn unique_strings(items: &[String]) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut out = vec![];
    for item in items {
        if !item.is_empty() && seen.insert(item.clone()) {
            out.push(item.clone());
        }
    }
    out
}

fn contains_string(items: &[String], target: &str) -> bool {
    items.iter().any(|i| i == target)
}

fn remove_string(items: &[String], target: &str) -> Vec<String> {
    items.iter().filter(|i| *i != target).cloned().collect()
}

fn read_pacman_ignore() -> (Vec<String>, Vec<String>, String) {
    let mut packages = vec![];
    let mut groups = vec![];
    for key in ["IgnorePkg", "IgnoreGroup"] {
        let (out, _, code) = run_capture(Duration::from_secs(8), &["pacman-conf", key]);
        if code != 0 {
            continue;
        }
        let values = split_policy(&out);
        if key == "IgnorePkg" {
            packages.extend(values);
        } else {
            groups.extend(values);
        }
    }
    if !packages.is_empty() || !groups.is_empty() {
        return (
            unique_strings(&packages),
            unique_strings(&groups),
            "effective pacman configuration (pacman-conf)".into(),
        );
    }
    let data = match fs::read_to_string("/etc/pacman.conf") {
        Ok(d) => d,
        Err(e) => {
            return (
                vec![],
                vec![],
                format!("unable to read /etc/pacman.conf: {e}"),
            )
        }
    };
    let mut current = String::new();
    for raw in data.lines() {
        let without_comment = raw.split('#').next().unwrap_or("");
        let line = without_comment.trim();
        if line.is_empty() {
            continue;
        }
        let mut fields: Vec<&str> = line.split_whitespace().collect();
        if fields.is_empty() {
            continue;
        }
        let key = fields[0];
        if key == "IgnorePkg" || key == "IgnoreGroup" {
            current = key.to_string();
            fields = fields[1..].to_vec();
        } else if (raw.starts_with(' ') || raw.starts_with('\t')) && !current.is_empty() {
            fields = line.split_whitespace().collect();
        } else {
            current.clear();
            continue;
        }
        if current == "IgnorePkg" {
            packages.extend(fields.into_iter().map(|s| s.to_string()));
        } else if current == "IgnoreGroup" {
            groups.extend(fields.into_iter().map(|s| s.to_string()));
        }
    }
    (
        unique_strings(&packages),
        unique_strings(&groups),
        "/etc/pacman.conf".into(),
    )
}

fn build_pacman_config(packages: &[String], groups: &[String]) -> Result<Vec<u8>, String> {
    let data = fs::read_to_string("/etc/pacman.conf").map_err(|e| e.to_string())?;
    let lines: Vec<&str> = data.split('\n').collect();
    let mut out: Vec<String> = Vec::with_capacity(lines.len() + 4);
    let mut in_options = false;
    let mut inserted = false;
    let mut skip_continuation = false;
    for raw in lines {
        let trim = raw.trim();
        if trim.starts_with('[') && trim.ends_with(']') {
            in_options = trim == "[options]";
            skip_continuation = false;
            if in_options && !inserted {
                out.push(raw.to_string());
                out.push(String::new());
                out.push("# Managed by PKGER".into());
                out.push(
                    "# Edit IgnorePkg and IgnoreGroup from PKGER instead of manual changes.".into(),
                );
                let pkgs = unique_strings(packages);
                if !pkgs.is_empty() {
                    out.push(format!("IgnorePkg = {}", pkgs.join(" ")));
                }
                let grps = unique_strings(groups);
                if !grps.is_empty() {
                    out.push(format!("IgnoreGroup = {}", grps.join(" ")));
                }
                out.push(String::new());
                inserted = true;
                continue;
            }
        }
        if in_options && (trim.starts_with("IgnorePkg") || trim.starts_with("IgnoreGroup")) {
            skip_continuation = true;
            continue;
        }
        if in_options && skip_continuation && (raw.starts_with(' ') || raw.starts_with('\t')) {
            continue;
        }
        skip_continuation = false;
        out.push(raw.to_string());
    }
    if !inserted {
        return Err("[options] section was not found in /etc/pacman.conf".into());
    }
    Ok(out.join("\n").into_bytes())
}

fn validate_sudo(secret: &[u8]) -> (bool, String) {
    let mut input = secret.to_vec();
    input.push(b'\n');
    let mut child = match Command::new("sudo")
        .args(["-S", "-p", "", "-v"])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) => return (false, e.to_string()),
    };
    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(&input);
    }
    for b in &mut input {
        *b = 0;
    }
    let output = child.wait_with_output();
    match output {
        Ok(o) if o.status.success() => (true, String::new()),
        Ok(o) => {
            let detail = String::from_utf8_lossy(&o.stderr).trim().to_string();
            (
                false,
                if detail.is_empty() {
                    "Sudo authentication failed. Verify your password and try again.".into()
                } else {
                    detail
                },
            )
        }
        Err(e) => (false, e.to_string()),
    }
}

fn prepare_aur_askpass(secret: &[u8]) -> Result<(String, String), String> {
    if secret.is_empty() {
        return Err("administrator password is not available".into());
    }
    let dir = std::env::temp_dir().join(format!(
        "pkger-auth-{}",
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    ));
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let pass_path = dir.join("password");
    let ask_path = dir.join("askpass");
    let wrapper_path = dir.join("sudo");
    let mut pass_data = secret.to_vec();
    pass_data.push(b'\n');
    fs::write(&pass_path, &pass_data).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&pass_path, fs::Permissions::from_mode(0o600));
    }
    let ask = format!("#!/bin/sh\ncat \"{}\"\n", pass_path.display());
    fs::write(&ask_path, ask).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&ask_path, fs::Permissions::from_mode(0o700));
    }
    let wrapper = "#!/bin/sh\nexec /usr/bin/sudo -A \"$@\"\n";
    fs::write(&wrapper_path, wrapper).map_err(|e| e.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&wrapper_path, fs::Permissions::from_mode(0o700));
    }
    Ok((
        dir.display().to_string(),
        format!("SUDO_ASKPASS={}", ask_path.display()),
    ))
}

fn package_icon(repository: &str, installed: bool) -> Image {
    let mut icon_name = "package-x-generic-symbolic";
    match repository.to_lowercase().as_str() {
        "aur" => icon_name = "applications-development-symbolic",
        "flatpak" | "appimage" => icon_name = "application-x-executable-symbolic",
        "installed" => icon_name = "emblem-default-symbolic",
        _ => {}
    }
    if installed {
        icon_name = "emblem-default-symbolic";
    }
    let image = Image::from_icon_name(icon_name);
    image.set_pixel_size(24);
    image.add_css_class("package-icon");
    image
}

fn repository_icon(repository: &str) -> Image {
    let mut icon_name = "folder-symbolic";
    match repository.to_lowercase().as_str() {
        "aur" => icon_name = "applications-development-symbolic",
        "flatpak" => icon_name = "application-x-executable-symbolic",
        "installed" => icon_name = "emblem-default-symbolic",
        _ => {}
    }
    let image = Image::from_icon_name(icon_name);
    image.set_pixel_size(20);
    image.add_css_class("repo-icon");
    image
}

fn new_scrolled(child: &impl IsA<gtk4::Widget>) -> ScrolledWindow {
    let sw = ScrolledWindow::new();
    sw.set_child(Some(child));
    sw.set_policy(PolicyType::Automatic, PolicyType::Automatic);
    sw
}



// ===========================================================================
// GTK4 GUI
// ===========================================================================

#[derive(Clone)]
struct GuiCommand {
    label: String,
    privileged: bool,
    auth_required: bool,
    args: Vec<String>,
    stdin: Vec<u8>,
    env: Vec<String>,
}

impl GuiCommand {
    fn new(label: &str, privileged: bool, args: Vec<String>) -> Self {
        Self {
            label: label.into(),
            privileged,
            auth_required: false,
            args,
            stdin: vec![],
            env: vec![],
        }
    }
    fn with_auth(mut self) -> Self {
        self.auth_required = true;
        self
    }
    fn with_stdin(mut self, stdin: Vec<u8>) -> Self {
        self.stdin = stdin;
        self
    }
}

fn needs_authorization(commands: &[GuiCommand]) -> bool {
    commands.iter().any(|c| c.privileged || c.auth_required)
}

fn is_root() -> bool {
    fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("Uid:"))
                .and_then(|l| l.split_whitespace().nth(1).map(|u| u == "0"))
        })
        .unwrap_or(false)
}

#[derive(Clone)]
enum UiMsg {
    SetStatus(String),
    SetBusy(bool),
    UpdateHome,
    RenderPackages,
    PopulateUpdates,
    PopulateRepos,
    PopulateRepoPackages,
    PopulateImages,
    RefreshLogs,
    SetPackageDetails(String),
    SetPackageOutput(String),
    SetRepoDetails(String),
    SetRepoOutput(String),
    SetNews(String),
    SetPacmanDetected(String),
    OperationFinished(String),
    RequestSudo {
        label: String,
        commands: Vec<GuiCommand>,
    },
    SudoAuthResult {
        ok: bool,
        detail: String,
        secret: Vec<u8>,
        label: String,
        commands: Vec<GuiCommand>,
    },
    DoasAuthResult {
        ok: bool,
        detail: String,
        label: String,
        commands: Vec<GuiCommand>,
    },
    ApplyDetectedIgnore {
        packages: Vec<String>,
        groups: Vec<String>,
    },
    SearchDone {
        count: usize,
    },
}

struct UiState {
    visible_pkgs: Vec<Package>,
    selected_index: i32,
    selected_packages: HashMap<String, bool>,
    local_package_paths: Vec<String>,
    repo_selected: HashMap<String, bool>,
    repo_visible_pkgs: Vec<RepoPackage>,
    selected_repo: String,
    selected_image: i32,
    pkg_show_limit: usize,
    repo_show_limit: usize,
    op_running: bool,
    auth_secret: Vec<u8>,
    operation_cleanup: Vec<String>,
    auth_dialog_open: bool,
}

impl UiState {
    fn new() -> Self {
        Self {
            visible_pkgs: vec![],
            selected_index: -1,
            selected_packages: HashMap::new(),
            local_package_paths: vec![],
            repo_selected: HashMap::new(),
            repo_visible_pkgs: vec![],
            selected_repo: String::new(),
            selected_image: -1,
            pkg_show_limit: UI_ROW_LIMIT,
            repo_show_limit: UI_ROW_LIMIT,
            op_running: false,
            auth_secret: vec![],
            operation_cleanup: vec![],
            auth_dialog_open: false,
        }
    }
}

struct GuiApp {
    core: Arc<Mutex<App>>,
    ui: RefCell<UiState>,
    package_checks: RefCell<HashMap<String, CheckButton>>,
    update_checks: RefCell<HashMap<usize, CheckButton>>,
    repo_package_checks: RefCell<HashMap<String, CheckButton>>,
    settings_checks: RefCell<HashMap<String, CheckButton>>,
    tx: Sender<UiMsg>,
    auth_dialog: RefCell<Option<Dialog>>,
    auth_password: RefCell<Option<Entry>>,
    auth_error: RefCell<Option<Label>>,
    window: ApplicationWindow,
    stack: Stack,
    title: Label,
    status: Label,
    progress: ProgressBar,
    home_health: Label,
    home_installed: Label,
    home_updates: Label,
    home_security: Label,
    home_cache: Label,
    home_kernel: Label,
    home_repos: Label,
    home_refresh: Label,
    home_uptime: Label,
    home_load: Label,
    home_disk: Label,
    home_ram: Label,
    search_entry: Entry,
    source_drop: DropDown,
    sort_drop: DropDown,
    filter_entry: Entry,
    installed_only: CheckButton,
    install_result_label: Label,
    local_package_entry: Entry,
    local_files_list: ListBox,
    local_files_count: Label,
    package_list: ListBox,
    package_details: TextView,
    package_output: TextView,
    update_list: ListBox,
    ignore_packages_entry: Entry,
    ignore_groups_entry: Entry,
    pacman_detected_label: Label,
    update_policy_label: Label,
    repo_list: ListBox,
    repo_packages: ListBox,
    repo_filter: Entry,
    repo_package_filter: Entry,
    repo_installed_only: CheckButton,
    repo_sort_drop: DropDown,
    repo_stats: Label,
    repo_details: TextView,
    repo_output: TextView,
    image_list: ListBox,
    image_details: TextView,
    log_view: TextView,
    news_view: TextView,
}

fn gui_main() {
    let application = Application::builder()
        .application_id("com.almezali.pkger.gtk4")
        .build();
    application.connect_activate(|app| {
        let gui = build_gui(app);
        gui.window.present();
        gui.load_startup();
    });
    application.run();
}

fn metric_card(title: &str, value: &Label) -> GtkBox {
    let box_ = GtkBox::new(Orientation::Vertical, 3);
    box_.add_css_class("card");
    box_.set_hexpand(true);
    let t = Label::new(Some(title));
    t.add_css_class("stat-title");
    t.set_xalign(0.0);
    value.add_css_class("stat-value");
    value.set_xalign(0.0);
    box_.append(&t);
    box_.append(value);
    box_
}

fn build_gui(application: &Application) -> Rc<GuiApp> {
    GuiApp::apply_css();
    let core = Arc::new(Mutex::new(App::new()));
    let (tx, rx) = mpsc::channel::<UiMsg>();

    let window = ApplicationWindow::builder()
        .application(application)
        .title(format!("PKGER v{APP_VERSION} — Arch Package Manager"))
        .default_width(1240)
        .default_height(820)
        .build();

    let root = GtkBox::new(Orientation::Horizontal, 0);
    root.add_css_class("window-root");
    window.set_child(Some(&root));

    let title = Label::new(Some("Home"));
    title.add_css_class("page-title");
    title.set_hexpand(true);
    title.set_xalign(0.0);
    let status = Label::new(Some("Ready"));
    status.set_xalign(0.0);
    status.set_hexpand(true);
    let progress = ProgressBar::new();
    progress.set_hexpand(true);
    progress.set_visible(false);

    let mk = || Label::new(Some("—"));
    let home_health = mk();
    let home_installed = mk();
    let home_updates = mk();
    let home_security = mk();
    let home_cache = mk();
    let home_kernel = mk();
    let home_repos = mk();
    let home_refresh = mk();
    let home_uptime = Label::new(Some("Uptime: —"));
    let home_load = Label::new(Some("Load average: —"));
    let home_disk = Label::new(Some("Disk free: —"));
    let home_ram = Label::new(Some("Memory used: —"));
    for l in [&home_uptime, &home_load, &home_disk, &home_ram] {
        l.add_css_class("metric-line");
        l.set_xalign(0.0);
    }

    let search_entry = Entry::new();
    search_entry.set_placeholder_text(Some("Search packages by name or description…"));
    search_entry.set_hexpand(true);
    let source_drop =
        DropDown::new(Some(StringList::new(INSTALL_SOURCES)), None::<gtk4::Expression>);
    let sort_drop = DropDown::new(Some(StringList::new(PACKAGE_SORTS)), None::<gtk4::Expression>);
    let filter_entry = Entry::new();
    filter_entry.set_placeholder_text(Some("Filter visible results…"));
    filter_entry.set_hexpand(true);
    let installed_only = CheckButton::with_label("Installed only");
    let install_result_label = Label::new(Some("0 results"));
    install_result_label.add_css_class("badge");
    let local_package_entry = Entry::new();
    local_package_entry.set_placeholder_text(Some("No local Arch packages selected"));
    local_package_entry.set_editable(false);
    local_package_entry.set_hexpand(true);
    let local_files_list = ListBox::new();
    local_files_list.set_selection_mode(SelectionMode::None);
    let local_files_count = Label::new(Some("No local packages selected"));
    local_files_count.add_css_class("subtitle");
    local_files_count.set_xalign(0.0);
    let package_list = ListBox::new();
    package_list.set_selection_mode(SelectionMode::Single);
    let package_details = TextView::new();
    package_details.set_editable(false);
    package_details.set_monospace(true);
    package_details.add_css_class("details");
    let package_output = TextView::new();
    package_output.set_editable(false);
    package_output.set_monospace(true);
    package_output.add_css_class("details");

    let update_list = ListBox::new();
    update_list.set_selection_mode(SelectionMode::None);
    let ignore_packages_entry = Entry::new();
    ignore_packages_entry.set_placeholder_text(Some("Ignore packages: firefox, linux"));
    ignore_packages_entry.set_hexpand(true);
    {
        let app = core.lock().unwrap();
        ignore_packages_entry.set_text(&app.settings.ignore_packages.join(", "));
    }
    let ignore_groups_entry = Entry::new();
    ignore_groups_entry.set_placeholder_text(Some("Ignore groups: plasma"));
    ignore_groups_entry.set_hexpand(true);
    {
        let app = core.lock().unwrap();
        ignore_groups_entry.set_text(&app.settings.ignore_groups.join(", "));
    }
    let pacman_detected_label =
        Label::new(Some("Pacman configuration has not been scanned yet."));
    pacman_detected_label.set_hexpand(true);
    pacman_detected_label.set_xalign(0.0);
    pacman_detected_label.set_wrap(true);
    let update_policy_label = Label::new(Some("Ignore rules apply to the next pacman update."));
    update_policy_label.add_css_class("subtitle");

    let repo_list = ListBox::new();
    repo_list.set_selection_mode(SelectionMode::Single);
    let repo_packages = ListBox::new();
    repo_packages.set_selection_mode(SelectionMode::Single);
    let repo_filter = Entry::new();
    repo_filter.set_placeholder_text(Some("Filter sources…"));
    repo_filter.set_hexpand(true);
    let repo_package_filter = Entry::new();
    repo_package_filter.set_placeholder_text(Some("Filter package names…"));
    repo_package_filter.set_hexpand(true);
    let repo_installed_only = CheckButton::with_label("Installed only");
    let repo_sort_drop =
        DropDown::new(Some(StringList::new(PACKAGE_SORTS)), None::<gtk4::Expression>);
    let repo_stats = Label::new(Some("Select a source"));
    repo_stats.add_css_class("badge");
    let repo_details = TextView::new();
    repo_details.set_editable(false);
    repo_details.set_monospace(true);
    repo_details.add_css_class("details");
    let repo_output = TextView::new();
    repo_output.set_editable(false);
    repo_output.set_monospace(true);
    repo_output.add_css_class("details");
    let image_list = ListBox::new();
    let image_details = TextView::new();
    image_details.set_editable(false);
    image_details.set_monospace(true);
    image_details.add_css_class("details");
    let log_view = TextView::new();
    log_view.set_editable(false);
    log_view.set_monospace(true);
    log_view.add_css_class("details");
    let news_view = TextView::new();
    news_view.set_editable(false);
    news_view.set_wrap_mode(WrapMode::Word);

    let stack = Stack::new();
    stack.set_transition_type(StackTransitionType::SlideLeftRight);
    stack.set_vexpand(true);
    stack.set_hexpand(true);

    let gui = Rc::new(GuiApp {
        core: core.clone(),
        ui: RefCell::new(UiState::new()),
        package_checks: RefCell::new(HashMap::new()),
        update_checks: RefCell::new(HashMap::new()),
        repo_package_checks: RefCell::new(HashMap::new()),
        settings_checks: RefCell::new(HashMap::new()),
        tx: tx.clone(),
        auth_dialog: RefCell::new(None),
        auth_password: RefCell::new(None),
        auth_error: RefCell::new(None),
        window: window.clone(),
        stack: stack.clone(),
        title: title.clone(),
        status: status.clone(),
        progress: progress.clone(),
        home_health: home_health.clone(),
        home_installed: home_installed.clone(),
        home_updates: home_updates.clone(),
        home_security: home_security.clone(),
        home_cache: home_cache.clone(),
        home_kernel: home_kernel.clone(),
        home_repos: home_repos.clone(),
        home_refresh: home_refresh.clone(),
        home_uptime: home_uptime.clone(),
        home_load: home_load.clone(),
        home_disk: home_disk.clone(),
        home_ram: home_ram.clone(),
        search_entry: search_entry.clone(),
        source_drop: source_drop.clone(),
        sort_drop: sort_drop.clone(),
        filter_entry: filter_entry.clone(),
        installed_only: installed_only.clone(),
        install_result_label: install_result_label.clone(),
        local_package_entry: local_package_entry.clone(),
        local_files_list: local_files_list.clone(),
        local_files_count: local_files_count.clone(),
        package_list: package_list.clone(),
        package_details: package_details.clone(),
        package_output: package_output.clone(),
        update_list: update_list.clone(),
        ignore_packages_entry: ignore_packages_entry.clone(),
        ignore_groups_entry: ignore_groups_entry.clone(),
        pacman_detected_label: pacman_detected_label.clone(),
        update_policy_label: update_policy_label.clone(),
        repo_list: repo_list.clone(),
        repo_packages: repo_packages.clone(),
        repo_filter: repo_filter.clone(),
        repo_package_filter: repo_package_filter.clone(),
        repo_installed_only: repo_installed_only.clone(),
        repo_sort_drop: repo_sort_drop.clone(),
        repo_stats: repo_stats.clone(),
        repo_details: repo_details.clone(),
        repo_output: repo_output.clone(),
        image_list: image_list.clone(),
        image_details: image_details.clone(),
        log_view: log_view.clone(),
        news_view: news_view.clone(),
    });

    {
        let g = gui.clone();
        let rx = RefCell::new(rx);
        glib::timeout_add_local(Duration::from_millis(50), move || {
            let mut drained = Vec::new();
            {
                let rx = rx.borrow();
                while let Ok(msg) = rx.try_recv() {
                    drained.push(msg);
                }
            }
            for msg in drained {
                g.handle_msg(msg);
            }
            glib::ControlFlow::Continue
        });
    }

    let side = GtkBox::new(Orientation::Vertical, 0);
    side.add_css_class("sidebar");
    let brand = GtkBox::new(Orientation::Vertical, 2);
    brand.add_css_class("brand");
    let bname = Label::new(Some("PKGER"));
    bname.add_css_class("brand-name");
    bname.set_xalign(0.0);
    let bver = Label::new(Some(&format!("v{APP_VERSION} · Rust + GTK4")));
    bver.add_css_class("brand-version");
    bver.set_xalign(0.0);
    brand.append(&bname);
    brand.append(&bver);
    side.append(&brand);
    let sections: &[(&str, &[(&str, &str)])] = &[
        ("MAIN", &[("home", "Home")]),
        ("PACKAGES", &[("install", "Install"), ("updates", "Updates")]),
        (
            "SYSTEM",
            &[
                ("repos", "Sources"),
                ("images", "AppImages"),
                ("tools", "Tools"),
                ("settings", "Settings"),
            ],
        ),
        ("MORE", &[("logs", "Logs"), ("news", "News")]),
    ];
    for (sec, items) in sections {
        let l = Label::new(Some(sec));
        l.add_css_class("nav-section");
        l.set_xalign(0.0);
        side.append(&l);
        for (id, lab) in *items {
            let b = Button::with_label(lab);
            b.add_css_class("nav-button");
            let g = gui.clone();
            let id = (*id).to_string();
            b.connect_clicked(move |_| g.switch_page(&id));
            side.append(&b);
        }
    }
    let spacer = GtkBox::new(Orientation::Vertical, 0);
    spacer.set_vexpand(true);
    side.append(&spacer);
    let foot = Label::new(Some(
        "Arch Linux package manager\npacman · AUR · Flatpak · AppImage",
    ));
    foot.add_css_class("brand-version");
    foot.set_xalign(0.0);
    foot.set_margin_top(10);
    foot.set_margin_bottom(8);
    foot.set_margin_start(10);
    side.append(&foot);
    root.append(&side);

    let mainb = GtkBox::new(Orientation::Vertical, 0);
    mainb.set_hexpand(true);
    mainb.set_vexpand(true);
    root.append(&mainb);

    let header = GtkBox::new(Orientation::Horizontal, 10);
    header.add_css_class("topbar");
    header.set_margin_start(20);
    header.set_margin_end(20);
    header.set_margin_top(14);
    header.set_margin_bottom(10);
    header.append(&title);
    let qs = Button::with_label("Search");
    qs.add_css_class("flat-button");
    {
        let g = gui.clone();
        qs.connect_clicked(move |_| {
            g.switch_page("install");
            g.search_entry.grab_focus();
        });
    }
    header.append(&qs);
    let rf = Button::with_label("Refresh");
    rf.add_css_class("flat-button");
    {
        let g = gui.clone();
        rf.connect_clicked(move |_| g.refresh_current());
    }
    header.append(&rf);
    mainb.append(&header);

    stack.add_titled(&gui.build_home_page(), Some("home"), "Home");
    stack.add_titled(&gui.build_install_page(), Some("install"), "Install");
    stack.add_titled(&gui.build_updates_page(), Some("updates"), "Updates");
    stack.add_titled(&gui.build_repos_page(), Some("repos"), "Sources");
    stack.add_titled(&gui.build_images_page(), Some("images"), "AppImages");
    stack.add_titled(&gui.build_tools_page(), Some("tools"), "Tools");
    stack.add_titled(&gui.build_settings_page(), Some("settings"), "Settings");
    stack.add_titled(&gui.build_logs_page(), Some("logs"), "Logs");
    stack.add_titled(&gui.build_news_page(), Some("news"), "News");
    mainb.append(&stack);

    let bottom = GtkBox::new(Orientation::Horizontal, 10);
    bottom.add_css_class("statusbar");
    bottom.set_margin_start(16);
    bottom.set_margin_end(16);
    bottom.set_margin_top(8);
    bottom.set_margin_bottom(10);
    bottom.append(&status);
    bottom.append(&progress);
    mainb.append(&bottom);

    gui.switch_page("home");
    gui
}

impl GuiApp {
    fn apply_css() {
        let css = include_str!("pkger.css");
        let provider = CssProvider::new();
        provider.load_from_data(css);
        if let Some(display) = gdk::Display::default() {
            gtk4::style_context_add_provider_for_display(
                &display,
                &provider,
                gtk4::STYLE_PROVIDER_PRIORITY_APPLICATION,
            );
        }
    }

    fn send(&self, msg: UiMsg) {
        let _ = self.tx.send(msg);
    }

    fn set_status(&self, text: &str) {
        self.status.set_text(text);
    }

    fn set_busy(&self, b: bool) {
        self.progress.set_visible(b);
        if b {
            self.progress.pulse();
        }
    }

    fn handle_msg(self: &Rc<Self>, msg: UiMsg) {
        match msg {
            UiMsg::SetStatus(s) => self.set_status(&s),
            UiMsg::SetBusy(b) => self.set_busy(b),
            UiMsg::UpdateHome => self.update_home(),
            UiMsg::RenderPackages => self.render_packages(),
            UiMsg::PopulateUpdates => self.populate_updates(),
            UiMsg::PopulateRepos => self.populate_repos(),
            UiMsg::PopulateRepoPackages => self.populate_repo_packages(),
            UiMsg::PopulateImages => self.populate_images(),
            UiMsg::RefreshLogs => self.refresh_logs(),
            UiMsg::SetPackageDetails(t) => self.package_details.buffer().set_text(&t),
            UiMsg::SetPackageOutput(t) => {
                self.package_output.buffer().set_text(&t);
                self.repo_output.buffer().set_text(&t);
            }
            UiMsg::SetRepoDetails(t) => self.repo_details.buffer().set_text(&t),
            UiMsg::SetRepoOutput(t) => self.repo_output.buffer().set_text(&t),
            UiMsg::SetNews(t) => self.news_view.buffer().set_text(&t),
            UiMsg::SetPacmanDetected(t) => self.pacman_detected_label.set_text(&t),
            UiMsg::OperationFinished(label) => {
                self.cleanup_operation_files();
                self.ui.borrow_mut().op_running = false;
                self.set_busy(false);
                self.set_status(&format!("{label} finished"));
            }
            UiMsg::RequestSudo { label, commands } => self.show_sudo_dialog(&label, commands),
            UiMsg::SudoAuthResult {
                ok,
                detail,
                secret,
                label,
                commands,
            } => {
                if ok {
                    self.ui.borrow_mut().auth_secret = secret;
                    self.ui.borrow_mut().auth_dialog_open = false;
                    if let Some(dialog) = self.auth_dialog.borrow_mut().take() {
                        dialog.close();
                    }
                    *self.auth_password.borrow_mut() = None;
                    *self.auth_error.borrow_mut() = None;
                    self.run_commands_async(&label, commands);
                } else {
                    if let Some(password) = self.auth_password.borrow().as_ref() {
                        password.set_sensitive(true);
                    }
                    if let Some(error) = self.auth_error.borrow().as_ref() {
                        error.set_text(&detail);
                    }
                    self.set_status("Authentication failed — try again");
                }
            }
            UiMsg::DoasAuthResult {
                ok,
                detail,
                label,
                commands,
            } => {
                if ok {
                    self.run_commands_async(&label, commands);
                } else {
                    self.end_operation();
                    self.set_busy(false);
                    self.set_status(&detail);
                }
            }
            UiMsg::ApplyDetectedIgnore { packages, groups } => {
                self.ignore_packages_entry.set_text(&packages.join(", "));
                self.ignore_groups_entry.set_text(&groups.join(", "));
                self.apply_pacman_ignore_rules(packages, groups);
            }
            UiMsg::SearchDone { count } => {
                self.render_packages();
                self.set_busy(false);
                self.set_status(&format!("Found {count} packages"));
            }
        }
    }

    fn switch_page(self: &Rc<Self>, page: &str) {
        let titles = [
            ("home", "Home"),
            ("install", "Install"),
            ("updates", "Updates"),
            ("repos", "Sources"),
            ("images", "AppImages"),
            ("tools", "Tools"),
            ("settings", "Settings"),
            ("logs", "Logs"),
            ("news", "News"),
        ];
        self.stack.set_visible_child_name(page);
        if let Some((_, t)) = titles.iter().find(|(k, _)| *k == page) {
            self.title.set_text(t);
        }
        match page {
            "home" => self.update_home(),
            "updates" => {
                self.populate_updates();
                self.detect_pacman_ignore_async(false);
            }
            "repos" => self.populate_repos(),
            "images" => {
                let empty = self.core.lock().unwrap().app_images.is_empty();
                if empty {
                    self.scan_images();
                } else {
                    self.populate_images();
                }
            }
            "logs" => self.refresh_logs(),
            "news" => self.load_news(),
            _ => {}
        }
    }

    fn load_startup(&self) {
        self.set_busy(true);
        self.set_status("Loading live system data…");
        let core = self.core.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let stats = system_stats();
            {
                let mut app = core.lock().unwrap();
                app.stats = stats;
                app.load_installed();
                app.load_repos();
            }
            let _ = tx.send(UiMsg::UpdateHome);
            let _ = tx.send(UiMsg::PopulateRepos);
            let _ = tx.send(UiMsg::SetStatus("Checking updates…".into()));
            {
                let mut app = core.lock().unwrap();
                app.load_updates(false);
            }
            let _ = tx.send(UiMsg::SetBusy(false));
            let _ = tx.send(UiMsg::SetStatus("Ready".into()));
            let _ = tx.send(UiMsg::UpdateHome);
            let _ = tx.send(UiMsg::PopulateUpdates);
        });
    }

    fn refresh_current(&self) {
        let page = self
            .stack
            .visible_child_name()
            .unwrap_or_else(|| "home".into());
        let core = self.core.clone();
        let tx = self.tx.clone();
        self.set_status("Refreshing…");
        match page.as_str() {
            "updates" => self.load_updates(false),
            "repos" => {
                thread::spawn(move || {
                    core.lock().unwrap().load_repos();
                    let _ = tx.send(UiMsg::PopulateRepos);
                    let _ = tx.send(UiMsg::SetStatus("Repositories refreshed".into()));
                });
            }
            "images" => self.scan_images(),
            "news" => self.load_news(),
            _ => {
                thread::spawn(move || {
                    {
                        let mut app = core.lock().unwrap();
                        app.stats = system_stats();
                        app.load_installed();
                    }
                    let _ = tx.send(UiMsg::UpdateHome);
                    let _ = tx.send(UiMsg::SetStatus("Ready".into()));
                });
            }
        }
    }

    fn build_home_page(self: &Rc<Self>) -> ScrolledWindow {
        let outer = GtkBox::new(Orientation::Vertical, 15);
        outer.set_margin_top(22);
        outer.set_margin_bottom(22);
        outer.set_margin_start(24);
        outer.set_margin_end(24);
        let welcome = Label::new(Some("Welcome to PKGER"));
        welcome.add_css_class("section-title");
        welcome.set_xalign(0.0);
        outer.append(&welcome);
        let sub = Label::new(Some(&format!(
            "{} — practical package management with real-time system insight",
            detect_distro()
        )));
        sub.add_css_class("subtitle");
        sub.set_xalign(0.0);
        outer.append(&sub);
        let grid = Grid::new();
        grid.set_column_spacing(10);
        grid.set_row_spacing(10);
        grid.set_hexpand(true);
        grid.attach(&metric_card("SYSTEM HEALTH", &self.home_health), 0, 0, 1, 1);
        grid.attach(
            &metric_card("INSTALLED PACKAGES", &self.home_installed),
            1,
            0,
            1,
            1,
        );
        grid.attach(
            &metric_card("PENDING UPDATES", &self.home_updates),
            2,
            0,
            1,
            1,
        );
        grid.attach(
            &metric_card("SECURITY UPDATES", &self.home_security),
            3,
            0,
            1,
            1,
        );
        grid.attach(&metric_card("PACMAN CACHE", &self.home_cache), 0, 1, 1, 1);
        grid.attach(&metric_card("KERNEL", &self.home_kernel), 1, 1, 1, 1);
        grid.attach(&metric_card("REPOSITORIES", &self.home_repos), 2, 1, 1, 1);
        grid.attach(&metric_card("LAST REFRESH", &self.home_refresh), 3, 1, 1, 1);
        outer.append(&grid);
        let info = GtkBox::new(Orientation::Vertical, 8);
        info.add_css_class("card");
        let headline = Label::new(Some("Live system overview"));
        headline.add_css_class("card-title");
        headline.set_xalign(0.0);
        info.append(&headline);
        info.append(&self.home_uptime);
        info.append(&self.home_load);
        info.append(&self.home_disk);
        info.append(&self.home_ram);
        outer.append(&info);
        let actions = GtkBox::new(Orientation::Horizontal, 8);
        for (lab, page) in [
            ("Review Updates", "updates"),
            ("Search Packages", "install"),
            ("Manage Sources", "repos"),
            ("Open Tools", "tools"),
        ] {
            let b = Button::with_label(lab);
            let g = self.clone();
            let page = page.to_string();
            b.connect_clicked(move |_| g.switch_page(&page));
            actions.append(&b);
        }
        outer.append(&actions);
        let note = Label::new(Some(
            "All values on this page are collected from the local system: pacman, /proc, df, and installed optional tools.",
        ));
        note.add_css_class("subtitle");
        note.set_wrap(true);
        note.set_xalign(0.0);
        outer.append(&note);
        let sw = new_scrolled(&outer);
        sw.set_vexpand(true);
        sw
    }

    fn build_install_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Vertical, 10);
        outer.set_margin_top(15);
        outer.set_margin_bottom(15);
        outer.set_margin_start(16);
        outer.set_margin_end(16);

        let header = GtkBox::new(Orientation::Horizontal, 8);
        header.add_css_class("search-box");
        header.append(&self.search_entry);
        {
            let g = self.clone();
            self.search_entry.connect_activate(move |_| g.do_search());
        }
        let search = Button::with_label("Search");
        search.add_css_class("suggested-action");
        {
            let g = self.clone();
            search.connect_clicked(move |_| g.do_search());
        }
        header.append(&search);
        outer.append(&header);

        let controls = GtkBox::new(Orientation::Horizontal, 8);
        controls.append(&Label::new(Some("Source")));
        controls.append(&self.source_drop);
        controls.append(&Label::new(Some("Sort")));
        {
            let g = self.clone();
            self.sort_drop.connect_notify_local(Some("selected"), move |_, _| {
                g.render_packages();
            });
        }
        controls.append(&self.sort_drop);
        {
            let g = self.clone();
            self.filter_entry.connect_changed(move |_| g.render_packages());
        }
        controls.append(&self.filter_entry);
        {
            let g = self.clone();
            self.installed_only
                .connect_toggled(move |_| g.render_packages());
        }
        controls.append(&self.installed_only);
        outer.append(&controls);

        let meta = GtkBox::new(Orientation::Horizontal, 8);
        let meta_label = Label::new(Some(
            "Select packages with the checkboxes. Search results stay responsive while commands run in the background.",
        ));
        meta_label.add_css_class("subtitle");
        meta_label.set_hexpand(true);
        meta_label.set_xalign(0.0);
        meta.append(&meta_label);
        meta.append(&self.install_result_label);
        outer.append(&meta);

        let body = GtkBox::new(Orientation::Horizontal, 12);
        body.set_vexpand(true);
        let left = GtkBox::new(Orientation::Vertical, 7);
        left.set_hexpand(true);
        left.set_vexpand(true);
        {
            let g = self.clone();
            self.package_list.connect_row_selected(move |_, row| {
                if let Some(row) = row {
                    if row.widget_name() == LOAD_MORE_NAME {
                        return;
                    }
                    let i = row.index();
                    let mut ui = g.ui.borrow_mut();
                    if i >= 0 && (i as usize) < ui.visible_pkgs.len() {
                        ui.selected_index = i;
                        let p = ui.visible_pkgs[i as usize].clone();
                        drop(ui);
                        g.show_package(p);
                    }
                }
            });
        }
        let sw = ScrolledWindow::new();
        sw.set_child(Some(&self.package_list));
        sw.set_vexpand(true);
        left.append(&sw);

        let select_row = GtkBox::new(Orientation::Horizontal, 7);
        let select_all = Button::with_label("Select all");
        {
            let g = self.clone();
            select_all.connect_clicked(move |_| g.set_package_selection(true));
        }
        let clear_all = Button::with_label("Clear");
        {
            let g = self.clone();
            clear_all.connect_clicked(move |_| g.set_package_selection(false));
        }
        let install = Button::with_label("Install selected");
        install.add_css_class("suggested-action");
        {
            let g = self.clone();
            install.connect_clicked(move |_| g.install_selected());
        }
        let remove = Button::with_label("Remove selected");
        remove.add_css_class("destructive-action");
        {
            let g = self.clone();
            remove.connect_clicked(move |_| g.remove_selected());
        }
        select_row.append(&select_all);
        select_row.append(&clear_all);
        select_row.append(&install);
        select_row.append(&remove);
        left.append(&select_row);

        let local_row = GtkBox::new(Orientation::Horizontal, 7);
        local_row.append(&self.local_package_entry);
        let browse = Button::with_label("Browse files");
        {
            let g = self.clone();
            browse.connect_clicked(move |_| g.browse_local_packages());
        }
        local_row.append(&browse);
        let clear_local = Button::with_label("Clear");
        {
            let g = self.clone();
            clear_local.connect_clicked(move |_| g.clear_local_packages());
        }
        local_row.append(&clear_local);
        left.append(&local_row);
        left.append(&self.local_files_count);
        let local_scroll = ScrolledWindow::new();
        local_scroll.set_child(Some(&self.local_files_list));
        local_scroll.set_size_request(-1, 92);
        local_scroll.add_css_class("local-files");
        left.append(&local_scroll);
        let local_install = Button::with_label("Install selected local packages");
        local_install.add_css_class("suggested-action");
        {
            let g = self.clone();
            local_install.connect_clicked(move |_| g.install_local_package());
        }
        left.append(&local_install);
        body.append(&left);

        let right = GtkBox::new(Orientation::Vertical, 7);
        right.set_size_request(385, -1);
        let dtitle = Label::new(Some("Package details"));
        dtitle.add_css_class("stat-title");
        dtitle.set_xalign(0.0);
        right.append(&dtitle);
        let ds = ScrolledWindow::new();
        ds.set_child(Some(&self.package_details));
        ds.set_vexpand(true);
        right.append(&ds);
        let ot = Label::new(Some("Operation output"));
        ot.add_css_class("stat-title");
        ot.set_xalign(0.0);
        right.append(&ot);
        let osw = ScrolledWindow::new();
        osw.set_child(Some(&self.package_output));
        osw.set_vexpand(true);
        right.append(&osw);
        body.append(&right);
        outer.append(&body);
        outer
    }
}

impl GuiApp {
    fn build_updates_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Vertical, 10);
        outer.set_margin_top(16);
        outer.set_margin_bottom(16);
        outer.set_margin_start(16);
        outer.set_margin_end(16);
        let row = GtkBox::new(Orientation::Horizontal, 8);
        let sum = Label::new(Some("No update scan yet"));
        sum.add_css_class("subtitle");
        sum.set_hexpand(true);
        sum.set_xalign(0.0);
        row.append(&sum);
        let items: &[(&str, Box<dyn Fn(&Rc<GuiApp>)>)] = &[];
        let _ = items;
        for (lab, action) in [
            ("Refresh", 0),
            ("Sync & refresh", 1),
            ("Select all", 2),
            ("Security", 3),
            ("Apply selected", 4),
        ] {
            let b = Button::with_label(lab);
            if lab == "Apply selected" {
                b.add_css_class("suggested-action");
            }
            let g = self.clone();
            b.connect_clicked(move |_| match action {
                0 => g.load_updates(false),
                1 => g.load_updates(true),
                2 => g.set_update_selection(true),
                3 => g.select_security(),
                4 => g.apply_updates(),
                _ => {}
            });
            row.append(&b);
        }
        outer.append(&row);
        let policy = GtkBox::new(Orientation::Horizontal, 7);
        policy.add_css_class("policy-bar");
        policy.append(&self.ignore_packages_entry);
        policy.append(&self.ignore_groups_entry);
        let save_policy = Button::with_label("Save policy");
        {
            let g = self.clone();
            save_policy.connect_clicked(move |_| g.save_update_policy());
        }
        policy.append(&save_policy);
        let manage_policy = Button::with_label("Manage ignore rules");
        {
            let g = self.clone();
            manage_policy.connect_clicked(move |_| g.show_ignore_manager());
        }
        policy.append(&manage_policy);
        policy.append(&self.update_policy_label);
        outer.append(&policy);
        let detected = GtkBox::new(Orientation::Horizontal, 8);
        detected.add_css_class("detection-card");
        detected.append(&self.pacman_detected_label);
        let detect_button = Button::with_label("Detect from pacman.conf");
        {
            let g = self.clone();
            detect_button.connect_clicked(move |_| g.detect_pacman_ignore_async(false));
        }
        detected.append(&detect_button);
        let apply_detected = Button::with_label("Use detected rules");
        {
            let g = self.clone();
            apply_detected.connect_clicked(move |_| g.detect_pacman_ignore_async(true));
        }
        detected.append(&apply_detected);
        outer.append(&detected);
        let sw = ScrolledWindow::new();
        sw.set_child(Some(&self.update_list));
        sw.set_vexpand(true);
        outer.append(&sw);
        outer
    }

    fn build_repos_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Horizontal, 12);
        outer.set_margin_top(16);
        outer.set_margin_bottom(16);
        outer.set_margin_start(16);
        outer.set_margin_end(16);

        let left = GtkBox::new(Orientation::Vertical, 8);
        left.set_size_request(300, -1);
        let title = Label::new(Some("Sources"));
        title.add_css_class("section-title");
        title.set_xalign(0.0);
        left.append(&title);
        let intro = Label::new(Some(
            "Compact repository navigator. Choose a source, then inspect package details below it.",
        ));
        intro.add_css_class("subtitle");
        intro.set_wrap(true);
        intro.set_xalign(0.0);
        left.append(&intro);
        let bar = GtkBox::new(Orientation::Horizontal, 6);
        {
            let g = self.clone();
            self.repo_filter
                .connect_changed(move |_| g.populate_repos());
        }
        bar.append(&self.repo_filter);
        let refresh = Button::with_label("Refresh");
        {
            let g = self.clone();
            refresh.connect_clicked(move |_| g.refresh_repos_async());
        }
        bar.append(&refresh);
        left.append(&bar);

        let repo_card = GtkBox::new(Orientation::Vertical, 4);
        repo_card.add_css_class("source-card");
        {
            let g = self.clone();
            self.repo_list.connect_row_selected(move |_, row| {
                if let Some(row) = row {
                    let keys = g.filtered_repo_names();
                    let i = row.index() as usize;
                    if i < keys.len() {
                        g.ui.borrow_mut().selected_repo = keys[i].clone();
                        g.ui.borrow_mut().repo_show_limit = UI_ROW_LIMIT;
                        g.ensure_repo_packages();
                    }
                }
            });
        }
        let rs = ScrolledWindow::new();
        rs.set_child(Some(&self.repo_list));
        rs.set_vexpand(true);
        repo_card.append(&rs);
        left.append(&repo_card);

        let detail_title = Label::new(Some("Package details"));
        detail_title.add_css_class("stat-title");
        detail_title.set_xalign(0.0);
        left.append(&detail_title);
        let ds = ScrolledWindow::new();
        ds.set_child(Some(&self.repo_details));
        ds.set_size_request(-1, 190);
        left.append(&ds);
        outer.append(&left);

        let right = GtkBox::new(Orientation::Vertical, 8);
        right.set_hexpand(true);
        right.set_vexpand(true);
        let head = GtkBox::new(Orientation::Horizontal, 8);
        let rt = Label::new(Some("Repository packages"));
        rt.add_css_class("section-title");
        rt.set_hexpand(true);
        rt.set_xalign(0.0);
        head.append(&rt);
        head.append(&self.repo_stats);
        right.append(&head);
        let controls = GtkBox::new(Orientation::Horizontal, 7);
        {
            let g = self.clone();
            self.repo_package_filter
                .connect_changed(move |_| g.populate_repo_packages());
        }
        controls.append(&self.repo_package_filter);
        {
            let g = self.clone();
            self.repo_sort_drop
                .connect_notify_local(Some("selected"), move |_, _| {
                    g.populate_repo_packages();
                });
        }
        controls.append(&self.repo_sort_drop);
        {
            let g = self.clone();
            self.repo_installed_only
                .connect_toggled(move |_| g.populate_repo_packages());
        }
        controls.append(&self.repo_installed_only);
        right.append(&controls);

        {
            let g = self.clone();
            self.repo_packages.connect_row_selected(move |_, row| {
                if let Some(row) = row {
                    if row.widget_name() == LOAD_MORE_NAME {
                        return;
                    }
                    let i = row.index() as usize;
                    let pkg = g.ui.borrow().repo_visible_pkgs.get(i).cloned();
                    if let Some(p) = pkg {
                        g.show_repo_package(p);
                    }
                }
            });
        }
        let ps = ScrolledWindow::new();
        ps.set_child(Some(&self.repo_packages));
        ps.set_vexpand(true);
        right.append(&ps);
        let actions = GtkBox::new(Orientation::Horizontal, 7);
        let all = Button::with_label("Select all");
        {
            let g = self.clone();
            all.connect_clicked(move |_| g.set_repo_selection(true));
        }
        let clear = Button::with_label("Clear");
        {
            let g = self.clone();
            clear.connect_clicked(move |_| g.set_repo_selection(false));
        }
        let install = Button::with_label("Install selected");
        install.add_css_class("suggested-action");
        {
            let g = self.clone();
            install.connect_clicked(move |_| g.install_repo_selected());
        }
        let remove = Button::with_label("Remove selected");
        remove.add_css_class("destructive-action");
        {
            let g = self.clone();
            remove.connect_clicked(move |_| g.remove_repo_selected());
        }
        let export = Button::with_label("Export");
        {
            let g = self.clone();
            export.connect_clicked(move |_| {
                let repo = g.ui.borrow().selected_repo.clone();
                g.core.lock().unwrap().export_repo(&repo);
                g.set_status("Repository list exported");
            });
        }
        actions.append(&all);
        actions.append(&clear);
        actions.append(&install);
        actions.append(&remove);
        actions.append(&export);
        right.append(&actions);
        let out_title = Label::new(Some("Operation output"));
        out_title.add_css_class("stat-title");
        out_title.set_xalign(0.0);
        right.append(&out_title);
        let osw = ScrolledWindow::new();
        osw.set_child(Some(&self.repo_output));
        osw.set_size_request(-1, 125);
        right.append(&osw);
        outer.append(&right);
        outer
    }

    fn build_images_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Vertical, 9);
        outer.set_margin_top(16);
        outer.set_margin_bottom(16);
        outer.set_margin_start(16);
        outer.set_margin_end(16);
        let row = GtkBox::new(Orientation::Horizontal, 8);
        let scan = Button::with_label("Scan locations");
        {
            let g = self.clone();
            scan.connect_clicked(move |_| g.scan_images());
        }
        let import_b = Button::with_label("Import from path");
        {
            let g = self.clone();
            import_b.connect_clicked(move |_| {
                g.set_status(
                    "Copy an AppImage to ~/Applications, then choose Scan locations.",
                );
            });
        }
        let launch = Button::with_label("Launch selected");
        {
            let g = self.clone();
            launch.connect_clicked(move |_| g.launch_image());
        }
        let remove = Button::with_label("Remove selected");
        remove.add_css_class("destructive-action");
        {
            let g = self.clone();
            remove.connect_clicked(move |_| g.remove_image());
        }
        row.append(&scan);
        row.append(&import_b);
        row.append(&launch);
        row.append(&remove);
        outer.append(&row);
        let hint = Label::new(Some(
            "PKGER scans ~/Applications, ~/.local/bin, and /opt for executable AppImage files.",
        ));
        hint.add_css_class("subtitle");
        hint.set_xalign(0.0);
        outer.append(&hint);
        {
            let g = self.clone();
            self.image_list.connect_row_selected(move |_, row| {
                if let Some(row) = row {
                    g.ui.borrow_mut().selected_image = row.index();
                    g.show_image();
                }
            });
        }
        let sw = ScrolledWindow::new();
        sw.set_child(Some(&self.image_list));
        sw.set_vexpand(true);
        outer.append(&sw);
        let ds = ScrolledWindow::new();
        ds.set_child(Some(&self.image_details));
        ds.set_vexpand(true);
        outer.append(&ds);
        outer
    }

    fn build_tools_page(self: &Rc<Self>) -> ScrolledWindow {
        let outer = GtkBox::new(Orientation::Vertical, 12);
        outer.set_margin_top(16);
        outer.set_margin_bottom(16);
        outer.set_margin_start(18);
        outer.set_margin_end(18);

        let header = GtkBox::new(Orientation::Horizontal, 10);
        let badge = Label::new(Some("TOOLS"));
        badge.add_css_class("tools-badge");
        header.append(&badge);
        let title_box = GtkBox::new(Orientation::Vertical, 2);
        let title = Label::new(Some("System Tools"));
        title.add_css_class("section-title");
        title.set_xalign(0.0);
        title_box.append(&title);
        let desc = Label::new(Some(
            "Safe, organized maintenance and diagnostics for your Arch Linux system.",
        ));
        desc.add_css_class("subtitle");
        desc.set_xalign(0.0);
        desc.set_wrap(true);
        title_box.append(&desc);
        header.append(&title_box);
        outer.append(&header);

        type ToolItem = (&'static str, &'static str, bool);
        let groups: &[(&str, &str, &[ToolItem])] = &[
            (
                "Package maintenance",
                "PKG",
                &[
                    ("upgrade", "Full system upgrade with pacman -Syu", true),
                    ("sync", "Synchronize package databases", true),
                    ("clean", "Remove unneeded cached packages", true),
                    ("deepclean", "Remove all cached package files", true),
                    ("filedb", "Synchronize package file databases", true),
                    ("orphans", "Find and review orphan packages", true),
                ],
            ),
            (
                "AUR, mirrors, and keys",
                "AUR",
                &[
                    (
                        "aur-upgrade",
                        "Upgrade installed AUR packages with yay or paru",
                        false,
                    ),
                    ("mirrors", "Refresh the mirror list with reflector", true),
                    ("keyring", "Refresh Arch Linux signing keys", true),
                    ("pkgfile", "Update the pkgfile database", true),
                ],
            ),
            (
                "System recovery",
                "FIX",
                &[
                    ("verify", "Verify installed package files", false),
                    ("db-check", "Check pacman database consistency", false),
                    ("mkinitcpio", "Regenerate initramfs images", true),
                    ("grub", "Regenerate the GRUB boot configuration", true),
                    (
                        "pacnew",
                        "Find pending .pacnew configuration files",
                        false,
                    ),
                ],
            ),
            (
                "Diagnostics and reports",
                "INFO",
                &[
                    (
                        "checkupdates",
                        "List available updates without changing the system",
                        false,
                    ),
                    ("foreign", "List foreign and AUR packages", false),
                    (
                        "native-explicit",
                        "List explicitly installed native packages",
                        false,
                    ),
                    ("failed-services", "Show failed systemd services", false),
                    ("journal-errors", "Show recent journal errors", false),
                    ("ip", "Show network interfaces", false),
                    ("df", "Show filesystem space", false),
                    ("memory", "Show memory usage", false),
                ],
            ),
            (
                "Flatpak and services",
                "APP",
                &[
                    ("flatpak-repair", "Repair Flatpak installations", false),
                    ("flatpak-update", "Update Flatpak applications", false),
                    ("flatpak-unused", "Remove unused Flatpak runtimes", false),
                    ("daemon-reload", "Reload the systemd manager", true),
                    (
                        "user-daemon-reload",
                        "Reload the user systemd manager",
                        false,
                    ),
                ],
            ),
        ];

        for (gtitle, icon, items) in groups {
            let section = GtkBox::new(Orientation::Vertical, 7);
            section.add_css_class("tool-section");
            let section_header = GtkBox::new(Orientation::Horizontal, 8);
            let section_icon = Label::new(Some(icon));
            section_icon.add_css_class("tool-section-icon");
            section_header.append(&section_icon);
            let section_title = Label::new(Some(gtitle));
            section_title.add_css_class("tool-section-title");
            section_title.set_hexpand(true);
            section_title.set_xalign(0.0);
            section_header.append(&section_title);
            let count = Label::new(Some(&format!("{} tools", items.len())));
            count.add_css_class("tool-count");
            section_header.append(&count);
            section.append(&section_header);

            for (name, description, privileged) in *items {
                let card = GtkBox::new(Orientation::Horizontal, 9);
                card.add_css_class("tool-item");
                let icon_l = Label::new(Some("•"));
                icon_l.add_css_class("tool-item-icon");
                card.append(&icon_l);
                let info = GtkBox::new(Orientation::Vertical, 2);
                info.set_hexpand(true);
                let name_l = Label::new(Some(name));
                name_l.add_css_class("tool-item-name");
                name_l.set_xalign(0.0);
                info.append(&name_l);
                let brief = Label::new(Some(description));
                brief.add_css_class("tool-item-description");
                brief.set_xalign(0.0);
                brief.set_ellipsize(EllipsizeMode::End);
                info.append(&brief);
                card.append(&info);
                if *privileged {
                    let access = Label::new(Some("ADMIN"));
                    access.add_css_class("tool-access");
                    card.append(&access);
                }
                let button = Button::with_label("Run");
                button.add_css_class("tool-run");
                let g = self.clone();
                let action = (*name).to_string();
                button.connect_clicked(move |_| g.run_tool(&action));
                card.append(&button);
                section.append(&card);
            }
            outer.append(&section);
        }
        let sw = new_scrolled(&outer);
        sw.set_vexpand(true);
        sw
    }

    fn build_settings_page(self: &Rc<Self>) -> ScrolledWindow {
        let outer = GtkBox::new(Orientation::Vertical, 13);
        outer.set_margin_top(20);
        outer.set_margin_bottom(20);
        outer.set_margin_start(24);
        outer.set_margin_end(24);
        let title = Label::new(Some("Settings"));
        title.add_css_class("section-title");
        title.set_xalign(0.0);
        outer.append(&title);
        let desc = Label::new(Some(
            "Control startup behavior, confirmations, and search coverage.",
        ));
        desc.add_css_class("subtitle");
        desc.set_xalign(0.0);
        outer.append(&desc);

        let settings = self.core.lock().unwrap().settings.clone();
        let items = [
            (
                "auto_check_updates",
                "Auto-check updates on startup",
                settings.auto_check_updates,
            ),
            (
                "show_security_alerts",
                "Show security alerts prominently",
                settings.show_security_alerts,
            ),
            (
                "confirm_install",
                "Confirm before installing packages",
                settings.confirm_install,
            ),
            (
                "confirm_remove",
                "Confirm before removing packages",
                settings.confirm_remove,
            ),
            (
                "search_official",
                "Include official pacman results",
                settings.search_official,
            ),
            ("search_aur", "Include AUR results", settings.search_aur),
            (
                "search_flatpak",
                "Include Flatpak results",
                settings.search_flatpak,
            ),
            (
                "search_appimage",
                "Include AppImage results",
                settings.search_appimage,
            ),
        ];
        self.settings_checks.borrow_mut().clear();
        for (key, label, value) in items {
            let c = CheckButton::with_label(label);
            c.set_active(value);
            self.settings_checks
                .borrow_mut()
                .insert(key.to_string(), c.clone());
            outer.append(&c);
        }
        let save = Button::with_label("Save settings");
        save.add_css_class("suggested-action");
        {
            let g = self.clone();
            save.connect_clicked(move |_| g.save_gui_settings());
        }
        outer.append(&save);
        let sw = new_scrolled(&outer);
        sw.set_vexpand(true);
        sw
    }

    fn build_logs_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Vertical, 8);
        outer.set_margin_top(12);
        outer.set_margin_bottom(12);
        outer.set_margin_start(12);
        outer.set_margin_end(12);
        let sw = ScrolledWindow::new();
        sw.set_child(Some(&self.log_view));
        sw.set_vexpand(true);
        outer.append(&sw);
        let row = GtkBox::new(Orientation::Horizontal, 8);
        let clear = Button::with_label("Clear");
        {
            let g = self.clone();
            clear.connect_clicked(move |_| {
                g.core.lock().unwrap().log_lines.clear();
                g.refresh_logs();
            });
        }
        let export = Button::with_label("Export log");
        {
            let g = self.clone();
            export.connect_clicked(move |_| {
                g.core.lock().unwrap().export_log(None);
                g.refresh_logs();
            });
        }
        row.append(&clear);
        row.append(&export);
        outer.append(&row);
        outer
    }

    fn build_news_page(self: &Rc<Self>) -> GtkBox {
        let outer = GtkBox::new(Orientation::Vertical, 8);
        outer.set_margin_top(12);
        outer.set_margin_bottom(12);
        outer.set_margin_start(12);
        outer.set_margin_end(12);
        let row = GtkBox::new(Orientation::Horizontal, 8);
        let t = Label::new(Some("Arch Linux News"));
        t.add_css_class("section-title");
        t.set_hexpand(true);
        t.set_xalign(0.0);
        let refresh = Button::with_label("Refresh");
        {
            let g = self.clone();
            refresh.connect_clicked(move |_| g.load_news());
        }
        row.append(&t);
        row.append(&refresh);
        outer.append(&row);
        let sw = ScrolledWindow::new();
        sw.set_child(Some(&self.news_view));
        sw.set_vexpand(true);
        outer.append(&sw);
        outer
    }
}

impl GuiApp {
    fn update_home(&self) {
        let app = self.core.lock().unwrap();
        let score = health_score(
            app.packages.len(),
            &app.updates,
            &app.held,
            &app.stats,
        );
        let mut sec = 0;
        for u in &app.updates {
            if severity(&u.name, &u.repository, &u.source) == "Security" {
                sec += 1;
            }
        }
        self.home_health.set_text(&format!("{score}%"));
        self.home_installed.set_text(&format!("{}", app.packages.len()));
        self.home_updates.set_text(&format!("{}", app.updates.len()));
        self.home_security.set_text(&format!("{sec}"));
        self.home_cache.set_text(&human_size(app.stats.cache_size));
        self.home_kernel
            .set_text(&truncate(&app.stats.kernel, 18));
        self.home_repos.set_text(&format!("{}", app.repos.len()));
        self.home_refresh.set_text(&now_hms());
        self.home_uptime
            .set_text(&format!("Uptime: {}", app.stats.uptime));
        self.home_load
            .set_text(&format!("Load average: {}", app.stats.load));
        self.home_disk
            .set_text(&format!("Disk free: {}", app.stats.disk_free));
        self.home_ram
            .set_text(&format!("Memory used: {}", app.stats.mem_used_pct));
    }

    fn do_search(self: &Rc<Self>) {
        let mut q = self.search_entry.text().trim().to_string();
        let mut idx = self.source_drop.selected() as usize;
        if idx >= INSTALL_SOURCE_KEYS.len() {
            idx = 0;
        }
        let mode = INSTALL_SOURCE_KEYS[idx];
        if mode == "installed" && q.is_empty() {
            q = "installed".into();
        } else if !q.is_empty() && mode != "unified" {
            q = format!("{q} {mode}");
        }
        if q.is_empty() {
            self.set_status(
                "Enter a query or select Installed/AppImage with an empty query",
            );
            return;
        }
        self.set_busy(true);
        self.set_status(&format!("Searching {}…", INSTALL_SOURCES[idx]));
        let core = self.core.clone();
        let tx = self.tx.clone();
        // We need to update visible_pkgs on main thread after search.
        // Store results via UiMsg::SearchDone and copy in handle — but visible_pkgs is in ui.
        // Use a channel message that includes results... or lock and copy in thread then message.
        // Simpler: store last_results in core, then on SearchDone copy to visible_pkgs.
        thread::spawn(move || {
            {
                let mut app = core.lock().unwrap();
                app.search(&q);
            }
            let count = core.lock().unwrap().last_results.len();
            let _ = tx.send(UiMsg::SearchDone { count });
        });
    }

    fn filtered_install_packages(&self) -> Vec<Package> {
        let mut items = {
            let results = self.core.lock().unwrap().last_results.clone();
            let searching = !self.search_entry.text().trim().is_empty();
            if !results.is_empty() || searching {
                results
            } else {
                self.ui.borrow().visible_pkgs.clone()
            }
        };
        let filter = self.filter_entry.text().trim().to_lowercase();
        let installed_only = self.installed_only.is_active();
        let sort_key = self.sort_drop.selected() as usize;
        items.retain(|p| {
            let hay = format!(
                "{} {} {} {}",
                p.name, p.description, p.repository, p.version
            )
            .to_lowercase();
            if !filter.is_empty() && !hay.contains(&filter) {
                return false;
            }
            if installed_only && !p.installed {
                return false;
            }
            true
        });
        items.sort_by(|a, b| match sort_key {
            1 => a
                .repository
                .to_lowercase()
                .cmp(&b.repository.to_lowercase()),
            2 => a.version.to_lowercase().cmp(&b.version.to_lowercase()),
            3 => b
                .installed
                .cmp(&a.installed)
                .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase())),
            _ => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
        });
        items
    }

    fn append_install_package_row(self: &Rc<Self>, p: &Package) {
        let row = ListBoxRow::new();
        row.add_css_class("package-row");
        let box_ = GtkBox::new(Orientation::Horizontal, 12);
        box_.add_css_class("package-card");
        let check = CheckButton::new();
        check.set_active(
            *self
                .ui
                .borrow()
                .selected_packages
                .get(&p.name)
                .unwrap_or(&false),
        );
        {
            let g = self.clone();
            let name = p.name.clone();
            check.connect_toggled(move |c| {
                let mut ui = g.ui.borrow_mut();
                if c.is_active() {
                    ui.selected_packages.insert(name.clone(), true);
                } else {
                    ui.selected_packages.remove(&name);
                }
            });
        }
        box_.append(&check);
        box_.append(&package_icon(&p.repository, p.installed));
        let info = GtkBox::new(Orientation::Vertical, 3);
        info.set_hexpand(true);
        let name = Label::new(Some(&p.name));
        name.add_css_class("result-name");
        name.set_xalign(0.0);
        info.append(&name);
        let mut desc = p.description.clone();
        if desc.trim().is_empty() {
            desc = "No description available".into();
        }
        desc = truncate(&desc, 92);
        let description = Label::new(Some(&desc));
        description.add_css_class("package-description");
        description.set_xalign(0.0);
        description.set_ellipsize(EllipsizeMode::End);
        info.append(&description);
        let badge_line = GtkBox::new(Orientation::Horizontal, 6);
        let source = Label::new(Some(&p.repository.to_uppercase()));
        source.add_css_class("source-badge");
        badge_line.append(&source);
        let state = Label::new(Some(&package_status(p)));
        state.add_css_class("status-badge");
        badge_line.append(&state);
        info.append(&badge_line);
        box_.append(&info);
        let meta = GtkBox::new(Orientation::Vertical, 3);
        meta.add_css_class("package-meta");
        let version = Label::new(Some(&p.version));
        version.add_css_class("version-label");
        version.set_xalign(1.0);
        meta.append(&version);
        let action = Label::new(Some(yes_no(p.installed)));
        action.add_css_class("result-meta");
        action.set_xalign(1.0);
        meta.append(&action);
        box_.append(&meta);
        row.set_child(Some(&box_));
        self.package_list.append(&row);
        self.package_checks
            .borrow_mut()
            .insert(p.name.clone(), check);
    }

    fn append_load_more_row(
        self: &Rc<Self>,
        list: &ListBox,
        shown: usize,
        total: usize,
        packages: bool,
    ) {
        let remaining = total.saturating_sub(shown);
        if remaining == 0 {
            return;
        }
        let next = remaining.min(UI_ROW_LIMIT);
        let row = ListBoxRow::new();
        row.set_widget_name(LOAD_MORE_NAME);
        row.set_selectable(false);
        row.add_css_class("load-more-row");
        let btn = Button::new();
        btn.add_css_class("load-more-button");
        btn.set_hexpand(true);
        let inner = GtkBox::new(Orientation::Vertical, 3);
        let title = Label::new(Some("Show more"));
        title.add_css_class("load-more-title");
        title.set_xalign(0.0);
        let meta = Label::new(Some(&format!(
            "Load {next} more packages  ·  {shown} of {total} visible"
        )));
        meta.add_css_class("load-more-meta");
        meta.set_xalign(0.0);
        inner.append(&title);
        inner.append(&meta);
        btn.set_child(Some(&inner));
        {
            let g = self.clone();
            btn.connect_clicked(move |_| {
                if packages {
                    g.show_more_packages();
                } else {
                    g.show_more_repo_packages();
                }
            });
        }
        row.set_child(Some(&btn));
        list.append(&row);
    }

    fn remove_trailing_load_more(&self, list: &ListBox) {
        let mut i = 0;
        while list.row_at_index(i).is_some() {
            i += 1;
        }
        if i == 0 {
            return;
        }
        if let Some(row) = list.row_at_index(i - 1) {
            if row.widget_name() == LOAD_MORE_NAME {
                list.remove(&row);
            }
        }
    }

    fn render_packages(self: &Rc<Self>) {
        self.ui.borrow_mut().pkg_show_limit = UI_ROW_LIMIT;
        let items = self.filtered_install_packages();
        let total = items.len();
        let limit = self.ui.borrow().pkg_show_limit.min(total);
        let shown: Vec<Package> = items.into_iter().take(limit).collect();
        self.ui.borrow_mut().visible_pkgs = shown.clone();
        while let Some(row) = self.package_list.row_at_index(0) {
            self.package_list.remove(&row);
        }
        self.package_checks.borrow_mut().clear();
        for p in &shown {
            self.append_install_package_row(p);
        }
        self.append_load_more_row(&self.package_list, shown.len(), total, true);
        self.install_result_label.set_text(&if total > shown.len() {
            format!("{} shown of {total}", shown.len())
        } else {
            format!("{} packages shown", shown.len())
        });
    }

    fn show_more_packages(self: &Rc<Self>) {
        self.remove_trailing_load_more(&self.package_list);
        let old_limit = self.ui.borrow().pkg_show_limit;
        self.ui.borrow_mut().pkg_show_limit = old_limit + UI_ROW_LIMIT;
        let items = self.filtered_install_packages();
        let total = items.len();
        let start = old_limit.min(total);
        let end = self.ui.borrow().pkg_show_limit.min(total);
        for p in &items[start..end] {
            self.append_install_package_row(p);
        }
        self.ui.borrow_mut().visible_pkgs = items[..end].to_vec();
        self.append_load_more_row(&self.package_list, end, total, true);
        self.install_result_label.set_text(&if total > end {
            format!("{end} shown of {total}")
        } else {
            format!("{end} packages shown")
        });
        self.set_status(&format!("Showing {end} of {total} packages"));
    }

    fn set_package_selection(self: &Rc<Self>, on: bool) {
        let names: Vec<String> = self
            .ui
            .borrow()
            .visible_pkgs
            .iter()
            .map(|p| p.name.clone())
            .collect();
        {
            let mut ui = self.ui.borrow_mut();
            for name in &names {
                if on {
                    ui.selected_packages.insert(name.clone(), true);
                } else {
                    ui.selected_packages.remove(name);
                }
            }
        }
        for name in &names {
            if let Some(c) = self.package_checks.borrow().get(name) {
                c.set_active(on);
            }
        }
    }

    fn show_package(&self, p: Package) {
        self.package_details.buffer().set_text(&format!(
            "Name: {}\nRepository: {}\nVersion: {}\nInstalled: {}\nDescription: {}\n\nLoading metadata…",
            p.name, p.repository, p.version, p.installed, p.description
        ));
        let tx = self.tx.clone();
        let p2 = p.clone();
        thread::spawn(move || {
            let out = if p2.repository == "flatpak" {
                run_capture(Duration::from_secs(35), &["flatpak", "info", &p2.name]).0
            } else if p2.repository == "aur" {
                let h = aur_helper();
                if h.is_empty() {
                    String::new()
                } else {
                    run_capture(Duration::from_secs(35), &[&h, "-Si", &p2.name]).0
                }
            } else {
                let mut o =
                    run_capture(Duration::from_secs(35), &["pacman", "-Si", &p2.name]).0;
                if o.is_empty() {
                    o = run_capture(Duration::from_secs(25), &["pacman", "-Qi", &p2.name]).0;
                }
                o
            };
            let out = if out.is_empty() {
                "No additional metadata available.".into()
            } else {
                out
            };
            let text = format!(
                "Name: {}\nRepository: {}\nVersion: {}\nInstalled: {}\nDescription: {}\n\n{}",
                p2.name, p2.repository, p2.version, p2.installed, p2.description, out
            );
            let _ = tx.send(UiMsg::SetPackageDetails(text));
        });
    }

    fn install_selected(self: &Rc<Self>) {
        {
            let mut ui = self.ui.borrow_mut();
            if ui.selected_packages.is_empty()
                && ui.selected_index >= 0
                && (ui.selected_index as usize) < ui.visible_pkgs.len()
            {
                let name = ui.visible_pkgs[ui.selected_index as usize].name.clone();
                ui.selected_packages.insert(name, true);
            }
        }
        if self.ui.borrow().selected_packages.is_empty() {
            self.set_status("Select one or more packages first");
            return;
        }
        let mut pac = vec![];
        let mut aur = vec![];
        let mut flat = vec![];
        {
            let ui = self.ui.borrow();
            for p in &ui.visible_pkgs {
                if !ui.selected_packages.get(&p.name).copied().unwrap_or(false) || p.installed
                {
                    continue;
                }
                match p.repository.as_str() {
                    "aur" => aur.push(p.name.clone()),
                    "flatpak" => flat.push(p.name.clone()),
                    _ => pac.push(p.name.clone()),
                }
            }
        }
        let mut cmds = vec![];
        if !pac.is_empty() {
            let mut args = vec![
                "pacman".into(),
                "-S".into(),
                "--noconfirm".into(),
            ];
            args.extend(pac);
            cmds.push(GuiCommand::new("Install official packages", true, args));
        }
        if !aur.is_empty() {
            let h = aur_helper();
            if h.is_empty() {
                self.set_status("Install yay or paru for AUR packages");
                return;
            }
            let mut args = vec![
                h,
                "-S".into(),
                "--needed".into(),
                "--noconfirm".into(),
                "--sudoloop".into(),
            ];
            args.extend(aur);
            cmds.push(
                GuiCommand::new("Install AUR packages", false, args).with_auth(),
            );
        }
        if !flat.is_empty() {
            let mut args = vec!["flatpak".into(), "install".into(), "-y".into()];
            args.extend(flat);
            cmds.push(GuiCommand::new("Install Flatpak packages", false, args));
        }
        self.execute_commands("Install selected packages", cmds);
    }

    fn remove_selected(self: &Rc<Self>) {
        {
            let mut ui = self.ui.borrow_mut();
            if ui.selected_packages.is_empty()
                && ui.selected_index >= 0
                && (ui.selected_index as usize) < ui.visible_pkgs.len()
            {
                let name = ui.visible_pkgs[ui.selected_index as usize].name.clone();
                ui.selected_packages.insert(name, true);
            }
        }
        if self.ui.borrow().selected_packages.is_empty() {
            self.set_status("Select one or more packages first");
            return;
        }
        let mut pac = vec![];
        let mut flat = vec![];
        {
            let ui = self.ui.borrow();
            for p in &ui.visible_pkgs {
                if ui.selected_packages.get(&p.name).copied().unwrap_or(false) && p.installed {
                    if p.repository == "flatpak" {
                        flat.push(p.name.clone());
                    } else {
                        pac.push(p.name.clone());
                    }
                }
            }
        }
        let mut cmds = vec![];
        if !pac.is_empty() {
            let mut args = vec![
                "pacman".into(),
                "-Rns".into(),
                "--noconfirm".into(),
            ];
            args.extend(pac);
            cmds.push(GuiCommand::new("Remove packages", true, args));
        }
        if !flat.is_empty() {
            let mut args = vec!["flatpak".into(), "uninstall".into(), "-y".into()];
            args.extend(flat);
            cmds.push(GuiCommand::new("Remove Flatpak packages", false, args));
        }
        self.execute_commands("Remove selected packages", cmds);
    }

    fn browse_local_packages(self: &Rc<Self>) {
        let chooser = FileChooserNative::new(
            Some("Select local Arch packages"),
            Some(&self.window),
            FileChooserAction::Open,
            Some("Select files"),
            Some("Cancel"),
        );
        chooser.set_select_multiple(true);
        let filter = FileFilter::new();
        filter.add_pattern("*.pkg.tar.zst");
        filter.add_pattern("*.pkg.tar.xz");
        filter.add_pattern("*.pkg.tar.gz");
        chooser.set_filter(&filter);
        let g = self.clone();
        chooser.connect_response(move |chooser, response| {
            if response != ResponseType::Accept {
                return;
            }
            let model = chooser.files();
            let n = model.n_items();
            let mut paths = vec![];
            for i in 0..n {
                if let Some(obj) = model.item(i) {
                    if let Ok(file) = obj.downcast::<gio::File>() {
                        if let Some(p) = file.path() {
                            paths.push(p.display().to_string());
                        }
                    }
                }
            }
            let count = paths.len();
            g.ui.borrow_mut().local_package_paths = paths;
            g.render_local_packages();
            g.set_status(&format!("Selected {count} local packages"));
        });
        chooser.show();
    }

    fn render_local_packages(&self) {
        let paths = self.ui.borrow().local_package_paths.clone();
        self.local_package_entry.set_text(&paths.join("; "));
        while let Some(row) = self.local_files_list.row_at_index(0) {
            self.local_files_list.remove(&row);
        }
        for p in &paths {
            let row = ListBoxRow::new();
            let name = Path::new(p)
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or(p);
            row.set_child(Some(&Label::new(Some(name))));
            self.local_files_list.append(&row);
        }
        if paths.is_empty() {
            self.local_files_count
                .set_text("No local packages selected");
        } else {
            self.local_files_count
                .set_text(&format!("{} local packages selected", paths.len()));
        }
    }

    fn clear_local_packages(&self) {
        self.ui.borrow_mut().local_package_paths.clear();
        self.render_local_packages();
        self.set_status("Local package selection cleared");
    }

    fn install_local_package(self: &Rc<Self>) {
        let paths = self.ui.borrow().local_package_paths.clone();
        if paths.is_empty() {
            self.set_status("Browse and select one or more local Arch packages first");
            return;
        }
        let mut args = vec!["pacman".into(), "-U".into(), "--noconfirm".into()];
        args.extend(paths);
        self.execute_commands(
            "Install local packages",
            vec![GuiCommand::new("Install local packages", true, args)],
        );
    }
}

impl GuiApp {
    fn begin_operation(&self, label: &str) -> bool {
        let mut ui = self.ui.borrow_mut();
        if ui.op_running {
            drop(ui);
            self.set_status(
                "Another operation is already running. Please wait for it to finish.",
            );
            return false;
        }
        ui.op_running = true;
        drop(ui);
        self.set_status(&format!("{label}…"));
        true
    }

    fn end_operation(&self) {
        self.ui.borrow_mut().op_running = false;
    }

    fn set_operation_cleanup(&self, paths: Vec<String>) {
        self.ui.borrow_mut().operation_cleanup = paths;
    }

    fn cleanup_operation_files(&self) {
        let paths = {
            let mut ui = self.ui.borrow_mut();
            std::mem::take(&mut ui.operation_cleanup)
        };
        for path in paths {
            let _ = fs::remove_file(&path);
            let _ = fs::remove_dir_all(&path);
        }
    }

    fn execute_gui(self: &Rc<Self>, label: &str, privileged: bool, args: Vec<String>) {
        self.execute_commands(
            label,
            vec![GuiCommand::new(label, privileged, args)],
        );
    }

    fn execute_commands(self: &Rc<Self>, label: &str, commands: Vec<GuiCommand>) {
        if commands.is_empty() {
            self.set_status("No eligible packages selected");
            return;
        }
        if !self.begin_operation(label) {
            self.cleanup_operation_files();
            return;
        }
        self.set_busy(true);
        if needs_authorization(&commands) && !is_root() {
            let tool = priv_tool();
            if tool.is_empty() {
                self.end_operation();
                self.set_busy(false);
                self.set_status("No sudo or doas found");
                return;
            }
            if tool == "sudo" {
                self.show_sudo_dialog(label, commands);
                return;
            }
            self.verify_doas_async(label, commands);
            return;
        }
        self.run_commands_async(label, commands);
    }

    fn verify_doas_async(self: &Rc<Self>, label: &str, commands: Vec<GuiCommand>) {
        let tx = self.tx.clone();
        let label = label.to_string();
        thread::spawn(move || {
            let (_, err, code) =
                run_capture(Duration::from_secs(8), &["doas", "-n", "true"]);
            let detail = if err.trim().is_empty() {
                "doas authorization is unavailable. Authenticate first or configure doas persist."
                    .into()
            } else {
                err.trim().to_string()
            };
            let _ = tx.send(UiMsg::DoasAuthResult {
                ok: code == 0,
                detail,
                label,
                commands,
            });
        });
    }

    fn show_sudo_dialog(self: &Rc<Self>, label: &str, commands: Vec<GuiCommand>) {
        if self.ui.borrow().auth_dialog_open {
            self.end_operation();
            self.set_busy(false);
            self.set_status("Authentication dialog is already open");
            return;
        }
        self.ui.borrow_mut().auth_dialog_open = true;
        let dialog = Dialog::builder()
            .title("Administrator authentication")
            .modal(true)
            .transient_for(&self.window)
            .default_width(500)
            .default_height(270)
            .build();
        let content = dialog.content_area();
        content.set_margin_top(20);
        content.set_margin_bottom(20);
        content.set_margin_start(22);
        content.set_margin_end(22);
        let card = GtkBox::new(Orientation::Vertical, 10);
        card.add_css_class("auth-card");
        let title = Label::new(Some("Administrator access required"));
        title.add_css_class("section-title");
        title.set_xalign(0.0);
        card.append(&title);
        let description = Label::new(Some(
            "PKGER will validate your password once, then continue the operation without placing the password in any command.",
        ));
        description.add_css_class("subtitle");
        description.set_wrap(true);
        description.set_xalign(0.0);
        card.append(&description);
        let operation = Label::new(Some(&format!("Operation: {label}")));
        operation.add_css_class("result-meta");
        operation.set_xalign(0.0);
        card.append(&operation);
        let password = Entry::new();
        password.set_placeholder_text(Some("Enter your user password"));
        password.set_visibility(false);
        password.set_input_purpose(InputPurpose::Password);
        password.set_hexpand(true);
        card.append(&password);
        let note = Label::new(Some(
            "The password is used only for sudo validation, then cleared. It is never saved, logged, or included in command arguments.",
        ));
        note.add_css_class("subtitle");
        note.set_wrap(true);
        note.set_xalign(0.0);
        card.append(&note);
        let error_label = Label::new(Some(""));
        error_label.add_css_class("warning");
        error_label.set_wrap(true);
        error_label.set_xalign(0.0);
        card.append(&error_label);
        content.append(&card);
        dialog.add_button("Cancel", ResponseType::Cancel);
        dialog.add_button("Authenticate", ResponseType::Ok);

        *self.auth_dialog.borrow_mut() = Some(dialog.clone());
        *self.auth_password.borrow_mut() = Some(password.clone());
        *self.auth_error.borrow_mut() = Some(error_label.clone());

        let g = self.clone();
        let label = label.to_string();
        let commands = commands.clone();
        let password_cb = password.clone();
        let error_cb = error_label.clone();
        dialog.connect_response(move |dialog, response| {
            if response != ResponseType::Ok {
                g.ui.borrow_mut().auth_dialog_open = false;
                *g.auth_dialog.borrow_mut() = None;
                *g.auth_password.borrow_mut() = None;
                *g.auth_error.borrow_mut() = None;
                dialog.close();
                g.cleanup_operation_files();
                g.end_operation();
                g.set_busy(false);
                g.set_status("Operation cancelled");
                return;
            }
            let secret = password_cb.text().as_bytes().to_vec();
            password_cb.set_text("");
            if secret.is_empty() {
                error_cb.set_text("Password cannot be empty.");
                return;
            }
            password_cb.set_sensitive(false);
            error_cb.set_text("Validating authorization…");
            let tx = g.tx.clone();
            let label2 = label.clone();
            let commands2 = commands.clone();
            thread::spawn(move || {
                let (ok, detail) = validate_sudo(&secret);
                let validated = if ok { secret } else { vec![] };
                let _ = tx.send(UiMsg::SudoAuthResult {
                    ok,
                    detail,
                    secret: validated,
                    label: label2,
                    commands: commands2,
                });
            });
        });
        dialog.present();
        password.grab_focus();
    }

    fn run_commands_async(self: &Rc<Self>, label: &str, mut commands: Vec<GuiCommand>) {
        let core = self.core.clone();
        let tx = self.tx.clone();
        let auth_secret = std::mem::take(&mut self.ui.borrow_mut().auth_secret);
        let label = label.to_string();

        thread::spawn(move || {
            let mut all = String::new();
            let tool = priv_tool();
            let mut auth_dir = String::new();
            let mut ask_env = String::new();
            if tool == "sudo" && !auth_secret.is_empty() {
                for command in &mut commands {
                    if command.auth_required || command.privileged {
                        if auth_dir.is_empty() {
                            match prepare_aur_askpass(&auth_secret) {
                                Ok((dir, env)) => {
                                    auth_dir = dir;
                                    ask_env = env;
                                }
                                Err(_) => continue,
                            }
                        }
                        command.env.push(ask_env.clone());
                        command.env.push(format!(
                            "PATH={}:{}",
                            auth_dir,
                            std::env::var("PATH").unwrap_or_default()
                        ));
                    }
                }
            }

            for command in &commands {
                let mut cmd_args = command.args.clone();
                if command.privileged && !is_root() {
                    if tool.is_empty() {
                        all.push_str("No sudo or doas found.\n");
                        continue;
                    }
                    let flag = if !command.env.is_empty() && tool == "sudo" {
                        "-A"
                    } else {
                        "-n"
                    };
                    let mut prefixed = vec![tool.clone(), flag.to_string()];
                    prefixed.extend(cmd_args);
                    cmd_args = prefixed;
                }
                all.push_str(&format!("$ {}\n", cmd_args.join(" ")));
                let args_ref: Vec<&str> = cmd_args.iter().map(|s| s.as_str()).collect();
                // Stream-ish: run with capture, update output
                let mut child = match Command::new(args_ref[0])
                    .args(&args_ref[1..])
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .envs(command.env.iter().filter_map(|e| {
                        e.split_once('=').map(|(k, v)| (k.to_string(), v.to_string()))
                    }))
                    .stdin(if command.stdin.is_empty() {
                        Stdio::null()
                    } else {
                        Stdio::piped()
                    })
                    .spawn()
                {
                    Ok(c) => c,
                    Err(e) => {
                        all.push_str(&format!("ERROR: {e}\n\n"));
                        continue;
                    }
                };
                if !command.stdin.is_empty() {
                    if let Some(mut stdin) = child.stdin.take() {
                        let _ = stdin.write_all(&command.stdin);
                    }
                }
                let output = child.wait_with_output();
                match output {
                    Ok(o) => {
                        all.push_str(&String::from_utf8_lossy(&o.stdout));
                        all.push_str(&String::from_utf8_lossy(&o.stderr));
                        if !o.status.success() {
                            all.push_str(&format!(
                                "ERROR: exit {}\n",
                                o.status.code().unwrap_or(1)
                            ));
                        }
                    }
                    Err(e) => all.push_str(&format!("ERROR: {e}\n")),
                }
                all.push('\n');
                let _ = tx.send(UiMsg::SetPackageOutput(all.clone()));
            }

            if !auth_dir.is_empty() {
                let _ = fs::remove_dir_all(&auth_dir);
            }
            {
                let mut app = core.lock().unwrap();
                app.append_log(&all);
            }
            let _ = tx.send(UiMsg::SetPackageOutput(all));
            let _ = tx.send(UiMsg::OperationFinished(label.clone()));
        });
    }
}

impl GuiApp {
    fn load_updates(&self, sync_db: bool) {
        self.set_busy(true);
        let core = self.core.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            core.lock().unwrap().load_updates(sync_db);
            let summary = {
                let app = core.lock().unwrap();
                format_update_summary(&app.updates)
            };
            let _ = tx.send(UiMsg::PopulateUpdates);
            let _ = tx.send(UiMsg::UpdateHome);
            let _ = tx.send(UiMsg::SetBusy(false));
            let _ = tx.send(UiMsg::SetStatus(summary));
        });
    }

    fn show_ignore_manager(self: &Rc<Self>) {
        let dialog = Dialog::builder()
            .title("Pacman ignore rules")
            .modal(true)
            .transient_for(&self.window)
            .default_width(760)
            .default_height(560)
            .build();
        let area = dialog.content_area();
        area.set_margin_top(18);
        area.set_margin_bottom(18);
        area.set_margin_start(18);
        area.set_margin_end(18);
        let root = GtkBox::new(Orientation::Vertical, 10);
        let header = Label::new(Some(
            "Manage packages and groups excluded from updates",
        ));
        header.add_css_class("section-title");
        header.set_xalign(0.0);
        root.append(&header);
        let hint = Label::new(Some(
            "Select entries to remove, or add comma-separated names. Changes are written directly to /etc/pacman.conf after administrator authentication.",
        ));
        hint.add_css_class("subtitle");
        hint.set_wrap(true);
        hint.set_xalign(0.0);
        root.append(&hint);
        let columns = GtkBox::new(Orientation::Horizontal, 12);
        let package_box = GtkBox::new(Orientation::Vertical, 6);
        package_box.set_hexpand(true);
        let group_box = GtkBox::new(Orientation::Vertical, 6);
        group_box.set_hexpand(true);
        let package_title = Label::new(Some("Ignored packages"));
        package_title.add_css_class("tool-name");
        package_title.set_xalign(0.0);
        package_box.append(&package_title);
        let group_title = Label::new(Some("Ignored groups"));
        group_title.add_css_class("tool-name");
        group_title.set_xalign(0.0);
        group_box.append(&group_title);
        let package_list = ListBox::new();
        package_list.set_selection_mode(SelectionMode::None);
        package_list.set_vexpand(true);
        let group_list = ListBox::new();
        group_list.set_selection_mode(SelectionMode::None);
        group_list.set_vexpand(true);

        let (pkgs, grps) = {
            let app = self.core.lock().unwrap();
            (
                app.settings.ignore_packages.clone(),
                app.settings.ignore_groups.clone(),
            )
        };
        let package_checks: RefCell<HashMap<String, CheckButton>> = RefCell::new(HashMap::new());
        let group_checks: RefCell<HashMap<String, CheckButton>> = RefCell::new(HashMap::new());
        for name in &pkgs {
            let c = CheckButton::with_label(name);
            package_list.append(&c);
            package_checks.borrow_mut().insert(name.clone(), c);
        }
        for name in &grps {
            let c = CheckButton::with_label(name);
            group_list.append(&c);
            group_checks.borrow_mut().insert(name.clone(), c);
        }
        let ps = ScrolledWindow::new();
        ps.set_child(Some(&package_list));
        ps.set_vexpand(true);
        package_box.append(&ps);
        let gs = ScrolledWindow::new();
        gs.set_child(Some(&group_list));
        gs.set_vexpand(true);
        group_box.append(&gs);
        package_box.append(&Label::new(Some("Add packages")));
        let add_packages = Entry::new();
        add_packages.set_placeholder_text(Some("linux, firefox"));
        package_box.append(&add_packages);
        group_box.append(&Label::new(Some("Add groups")));
        let add_groups = Entry::new();
        add_groups.set_placeholder_text(Some("gnome, plasma"));
        group_box.append(&add_groups);
        columns.append(&package_box);
        columns.append(&group_box);
        root.append(&columns);
        area.append(&root);
        dialog.add_button("Remove selected", ResponseType::Apply);
        dialog.add_button("Cancel", ResponseType::Cancel);
        dialog.add_button("Save changes", ResponseType::Ok);

        let g = self.clone();
        let package_checks = Rc::new(package_checks);
        let group_checks = Rc::new(group_checks);
        let pkgs0 = pkgs.clone();
        let grps0 = grps.clone();
        dialog.connect_response(move |dialog, response| {
            if response == ResponseType::Cancel {
                dialog.close();
                return;
            }
            if response == ResponseType::Apply {
                let mut packages = vec![];
                let mut groups = vec![];
                for (name, c) in package_checks.borrow().iter() {
                    if !c.is_active() {
                        packages.push(name.clone());
                    }
                }
                for (name, c) in group_checks.borrow().iter() {
                    if !c.is_active() {
                        groups.push(name.clone());
                    }
                }
                g.apply_pacman_ignore_rules(packages, groups);
                dialog.close();
                return;
            }
            if response == ResponseType::Ok {
                let mut packages = pkgs0.clone();
                let mut groups = grps0.clone();
                for item in split_policy(&add_packages.text()) {
                    if !contains_string(&packages, &item) {
                        packages.push(item);
                    }
                }
                for item in split_policy(&add_groups.text()) {
                    if !contains_string(&groups, &item) {
                        groups.push(item);
                    }
                }
                for (name, c) in package_checks.borrow().iter() {
                    if c.is_active() {
                        packages = remove_string(&packages, name);
                    }
                }
                for (name, c) in group_checks.borrow().iter() {
                    if c.is_active() {
                        groups = remove_string(&groups, name);
                    }
                }
                g.ignore_packages_entry
                    .set_text(&packages.join(", "));
                g.ignore_groups_entry.set_text(&groups.join(", "));
                g.apply_pacman_ignore_rules(packages, groups);
                g.set_status("Pacman ignore rules saved");
                dialog.close();
            }
        });
        dialog.present();
    }

    fn detect_pacman_ignore_async(&self, use_detected: bool) {
        self.pacman_detected_label
            .set_text("Scanning pacman configuration…");
        let tx = self.tx.clone();
        thread::spawn(move || {
            let (packages, groups, source) = read_pacman_ignore();
            let msg = if packages.is_empty() && groups.is_empty() {
                format!("No IgnorePkg or IgnoreGroup entries were found in {source}.")
            } else {
                format!(
                    "Detected from {source}: {} ignored package(s), {} ignored group(s).",
                    packages.len(),
                    groups.len()
                )
            };
            let _ = tx.send(UiMsg::SetPacmanDetected(msg));
            if use_detected {
                let _ = tx.send(UiMsg::ApplyDetectedIgnore { packages, groups });
                let _ = tx.send(UiMsg::SetStatus(
                    "Effective pacman ignore rules imported into PKGER".into(),
                ));
            }
        });
    }

    fn save_update_policy(self: &Rc<Self>) {
        let packages = split_policy(&self.ignore_packages_entry.text());
        let groups = split_policy(&self.ignore_groups_entry.text());
        self.apply_pacman_ignore_rules(packages, groups);
    }

    fn apply_pacman_ignore_rules(self: &Rc<Self>, packages: Vec<String>, groups: Vec<String>) {
        let data = match build_pacman_config(&packages, &groups) {
            Ok(d) => d,
            Err(e) => {
                self.set_status(&format!("Cannot prepare pacman.conf: {e}"));
                return;
            }
        };
        {
            let mut app = self.core.lock().unwrap();
            app.settings.ignore_packages = unique_strings(&packages);
            app.settings.ignore_groups = unique_strings(&groups);
            app.save_settings();
        }
        let commands = vec![
            GuiCommand::new(
                "Backup pacman.conf",
                true,
                vec![
                    "cp".into(),
                    "-a".into(),
                    "/etc/pacman.conf".into(),
                    "/etc/pacman.conf.pkger.bak".into(),
                ],
            ),
            GuiCommand::new(
                "Write pacman.conf",
                true,
                vec!["sh".into(), "-c".into(), "cat > /etc/pacman.conf".into()],
            )
            .with_stdin(data),
        ];
        self.execute_commands("Apply pacman ignore rules", commands);
        self.set_status("Writing IgnorePkg and IgnoreGroup directly to /etc/pacman.conf…");
    }

    fn populate_updates(self: &Rc<Self>) {
        while let Some(row) = self.update_list.row_at_index(0) {
            self.update_list.remove(&row);
        }
        self.update_checks.borrow_mut().clear();
        let updates = self.core.lock().unwrap().updates.clone();
        for (i, u) in updates.iter().enumerate() {
            let row = ListBoxRow::new();
            let box_ = GtkBox::new(Orientation::Horizontal, 8);
            box_.add_css_class("result-row");
            let c = CheckButton::new();
            c.set_active(u.selected);
            {
                let core = self.core.clone();
                let idx = i;
                c.connect_toggled(move |btn| {
                    if let Ok(mut app) = core.lock() {
                        if let Some(u) = app.updates.get_mut(idx) {
                            u.selected = btn.is_active();
                        }
                    }
                });
            }
            let n = Label::new(Some(&format!("{}  {} → {}", u.name, u.from, u.to)));
            n.set_xalign(0.0);
            n.set_hexpand(true);
            let s = Label::new(Some(severity(&u.name, &u.repository, &u.source)));
            s.add_css_class("result-meta");
            box_.append(&c);
            box_.append(&n);
            box_.append(&s);
            row.set_child(Some(&box_));
            self.update_list.append(&row);
            self.update_checks.borrow_mut().insert(i, c);
        }
    }

    fn set_update_selection(&self, on: bool) {
        {
            let mut app = self.core.lock().unwrap();
            for u in &mut app.updates {
                u.selected = on;
            }
        }
        for c in self.update_checks.borrow().values() {
            c.set_active(on);
        }
    }

    fn select_security(&self) {
        let updates = self.core.lock().unwrap().updates.clone();
        {
            let mut app = self.core.lock().unwrap();
            for (i, u) in updates.iter().enumerate() {
                let on = severity(&u.name, &u.repository, &u.source) == "Security";
                if let Some(uu) = app.updates.get_mut(i) {
                    uu.selected = on;
                }
                if let Some(c) = self.update_checks.borrow().get(&i) {
                    c.set_active(on);
                }
            }
        }
    }

    fn apply_updates(self: &Rc<Self>) {
        let (pac, flat, ignore_pkgs, ignore_grps) = {
            let app = self.core.lock().unwrap();
            let mut pac = vec![];
            let mut flat = vec![];
            for u in &app.updates {
                if u.selected {
                    if u.source == "flatpak" {
                        flat.push(u.name.clone());
                    } else {
                        pac.push(u.name.clone());
                    }
                }
            }
            (
                pac,
                flat,
                app.settings.ignore_packages.clone(),
                app.settings.ignore_groups.clone(),
            )
        };
        let mut commands = vec![];
        if !pac.is_empty() {
            let mut args = vec!["pacman".into(), "-S".into(), "--noconfirm".into()];
            for name in &ignore_pkgs {
                args.push("--ignore".into());
                args.push(name.clone());
            }
            for name in &ignore_grps {
                args.push("--ignoregroup".into());
                args.push(name.clone());
            }
            args.extend(pac);
            commands.push(GuiCommand::new("Apply pacman updates", true, args));
        }
        if !flat.is_empty() {
            let mut args = vec!["flatpak".into(), "update".into(), "-y".into()];
            args.extend(flat);
            commands.push(GuiCommand::new("Apply Flatpak updates", false, args));
        }
        self.execute_commands("Apply selected updates", commands);
    }

    fn refresh_repos_async(&self) {
        self.set_busy(true);
        self.set_status("Refreshing repositories…");
        let core = self.core.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            core.lock().unwrap().load_repos();
            let _ = tx.send(UiMsg::PopulateRepos);
            let _ = tx.send(UiMsg::SetBusy(false));
            let _ = tx.send(UiMsg::SetStatus("Repositories refreshed".into()));
        });
    }

    fn repo_names(&self) -> Vec<String> {
        let app = self.core.lock().unwrap();
        let mut keys: Vec<String> = app.repos.keys().cloned().collect();
        keys.sort();
        keys
    }

    fn filtered_repo_names(&self) -> Vec<String> {
        let q = self.repo_filter.text().trim().to_lowercase();
        self.repo_names()
            .into_iter()
            .filter(|k| q.is_empty() || k.to_lowercase().contains(&q))
            .collect()
    }

    fn populate_repos(self: &Rc<Self>) {
        while let Some(row) = self.repo_list.row_at_index(0) {
            self.repo_list.remove(&row);
        }
        let names = self.filtered_repo_names();
        let app = self.core.lock().unwrap();
        for name in &names {
            let row = ListBoxRow::new();
            let card = GtkBox::new(Orientation::Vertical, 3);
            card.add_css_class("source-row");
            let line = GtkBox::new(Orientation::Horizontal, 8);
            line.append(&repository_icon(name));
            let label = Label::new(Some(name));
            label.add_css_class("result-name");
            label.set_xalign(0.0);
            label.set_hexpand(true);
            let badge = Label::new(Some(classify_repo(name)));
            badge.add_css_class("badge");
            line.append(&label);
            line.append(&badge);
            card.append(&line);
            let loaded = app.repos_loaded.contains(name);
            let count_n = app.repos.get(name).map(|v| v.len()).unwrap_or(0);
            let count = Label::new(Some(&if loaded {
                format!("{count_n} available packages")
            } else {
                "Select to load packages".into()
            }));
            count.add_css_class("result-meta");
            count.set_xalign(0.0);
            card.append(&count);
            row.set_child(Some(&card));
            self.repo_list.append(&row);
        }
        drop(app);
        let selected = self.ui.borrow().selected_repo.clone();
        if !selected.is_empty() && self.core.lock().unwrap().repos_loaded.contains(&selected) {
            self.populate_repo_packages();
        }
    }

    fn ensure_repo_packages(self: &Rc<Self>) {
        let repo = self.ui.borrow().selected_repo.clone();
        if repo.is_empty() {
            return;
        }
        if self.core.lock().unwrap().repos_loaded.contains(&repo) {
            self.populate_repo_packages();
            return;
        }
        self.repo_stats.set_text("Loading packages…");
        self.set_busy(true);
        self.set_status(&format!("Loading {repo}…"));
        let core = self.core.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            core.lock().unwrap().load_repo_packages(&repo);
            let _ = tx.send(UiMsg::PopulateRepoPackages);
            let _ = tx.send(UiMsg::SetBusy(false));
            let _ = tx.send(UiMsg::SetStatus("Ready".into()));
        });
    }

    fn filtered_repo_packages(&self) -> (Vec<RepoPackage>, usize) {
        let selected_repo = self.ui.borrow().selected_repo.clone();
        let mut list = self
            .core
            .lock()
            .unwrap()
            .repos
            .get(&selected_repo)
            .cloned()
            .unwrap_or_default();
        let filter = self.repo_package_filter.text().trim().to_lowercase();
        let installed_only = self.repo_installed_only.is_active();
        let sort_key = self.repo_sort_drop.selected() as usize;
        let mut installed = 0;
        list.retain(|p| {
            if p.installed {
                installed += 1;
            }
            if !filter.is_empty()
                && !format!("{} {}", p.name, p.version)
                    .to_lowercase()
                    .contains(&filter)
            {
                return false;
            }
            if installed_only && !p.installed {
                return false;
            }
            true
        });
        list.sort_by(|a, b| match sort_key {
            1 => a.version.cmp(&b.version),
            3 => b
                .installed
                .cmp(&a.installed)
                .then_with(|| a.name.cmp(&b.name)),
            _ => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
        });
        (list, installed)
    }

    fn append_repo_package_row(self: &Rc<Self>, p: &RepoPackage, selected_repo: &str) {
        let row = ListBoxRow::new();
        let box_ = GtkBox::new(Orientation::Horizontal, 10);
        box_.add_css_class("package-card");
        let check = CheckButton::new();
        check.set_active(
            *self
                .ui
                .borrow()
                .repo_selected
                .get(&p.name)
                .unwrap_or(&false),
        );
        {
            let g = self.clone();
            let name = p.name.clone();
            check.connect_toggled(move |c| {
                let mut ui = g.ui.borrow_mut();
                if c.is_active() {
                    ui.repo_selected.insert(name.clone(), true);
                } else {
                    ui.repo_selected.remove(&name);
                }
            });
        }
        box_.append(&check);
        box_.append(&package_icon(selected_repo, p.installed));
        let info = GtkBox::new(Orientation::Vertical, 2);
        info.set_hexpand(true);
        let name = Label::new(Some(&p.name));
        name.add_css_class("result-name");
        name.set_xalign(0.0);
        info.append(&name);
        let desc = Label::new(Some("Repository package"));
        desc.add_css_class("package-description");
        desc.set_xalign(0.0);
        info.append(&desc);
        box_.append(&info);
        let meta = GtkBox::new(Orientation::Vertical, 2);
        let version = Label::new(Some(&p.version));
        version.add_css_class("version-label");
        version.set_xalign(1.0);
        meta.append(&version);
        let state = Label::new(Some(yes_no(p.installed)));
        state.add_css_class("result-meta");
        state.set_xalign(1.0);
        meta.append(&state);
        box_.append(&meta);
        row.set_child(Some(&box_));
        self.repo_packages.append(&row);
        self.repo_package_checks
            .borrow_mut()
            .insert(p.name.clone(), check);
    }

    fn populate_repo_packages(self: &Rc<Self>) {
        let selected_repo = self.ui.borrow().selected_repo.clone();
        if selected_repo.is_empty() {
            return;
        }
        if !self
            .core
            .lock()
            .unwrap()
            .repos_loaded
            .contains(&selected_repo)
        {
            self.ensure_repo_packages();
            return;
        }
        self.ui.borrow_mut().repo_show_limit = UI_ROW_LIMIT;
        let (list, installed) = self.filtered_repo_packages();
        let total = list.len();
        let limit = self.ui.borrow().repo_show_limit.min(total);
        let shown: Vec<RepoPackage> = list.into_iter().take(limit).collect();
        self.ui.borrow_mut().repo_visible_pkgs = shown.clone();
        while let Some(row) = self.repo_packages.row_at_index(0) {
            self.repo_packages.remove(&row);
        }
        self.repo_package_checks.borrow_mut().clear();
        for p in &shown {
            self.append_repo_package_row(p, &selected_repo);
        }
        self.append_load_more_row(&self.repo_packages, shown.len(), total, false);
        self.repo_stats.set_text(&if total > shown.len() {
            format!(
                "{} shown of {total} · {installed} installed",
                shown.len()
            )
        } else {
            format!("{} shown · {installed} installed", shown.len())
        });
    }

    fn show_more_repo_packages(self: &Rc<Self>) {
        self.remove_trailing_load_more(&self.repo_packages);
        let selected_repo = self.ui.borrow().selected_repo.clone();
        let old_limit = self.ui.borrow().repo_show_limit;
        self.ui.borrow_mut().repo_show_limit = old_limit + UI_ROW_LIMIT;
        let (list, installed) = self.filtered_repo_packages();
        let total = list.len();
        let start = old_limit.min(total);
        let end = self.ui.borrow().repo_show_limit.min(total);
        for p in &list[start..end] {
            self.append_repo_package_row(p, &selected_repo);
        }
        self.ui.borrow_mut().repo_visible_pkgs = list[..end].to_vec();
        self.append_load_more_row(&self.repo_packages, end, total, false);
        self.repo_stats.set_text(&if total > end {
            format!("{end} shown of {total} · {installed} installed")
        } else {
            format!("{end} shown · {installed} installed")
        });
        self.set_status(&format!("Showing {end} of {total} packages in {selected_repo}"));
    }

    fn set_repo_selection(self: &Rc<Self>, on: bool) {
        let names: Vec<String> = self
            .ui
            .borrow()
            .repo_visible_pkgs
            .iter()
            .map(|p| p.name.clone())
            .collect();
        {
            let mut ui = self.ui.borrow_mut();
            for name in &names {
                if on {
                    ui.repo_selected.insert(name.clone(), true);
                } else {
                    ui.repo_selected.remove(name);
                }
            }
        }
        for name in &names {
            if let Some(c) = self.repo_package_checks.borrow().get(name) {
                c.set_active(on);
            }
        }
    }

    fn show_repo_package(&self, p: RepoPackage) {
        let repo = self.ui.borrow().selected_repo.clone();
        self.repo_details.buffer().set_text(&format!(
            "Name: {}\nRepository: {}\nVersion: {}\nInstalled: {}\n\nLoading metadata…",
            p.name, repo, p.version, p.installed
        ));
        let tx = self.tx.clone();
        let name = p.name.clone();
        let ver = p.version.clone();
        let inst = p.installed;
        thread::spawn(move || {
            let mut out = run_capture(Duration::from_secs(35), &["pacman", "-Si", &name]).0;
            if out.is_empty() {
                out = run_capture(Duration::from_secs(25), &["pacman", "-Qi", &name]).0;
            }
            if out.is_empty() {
                out = "No additional metadata available.".into();
            }
            let text = format!(
                "Name: {name}\nRepository: {repo}\nVersion: {ver}\nInstalled: {inst}\n\n{out}"
            );
            let _ = tx.send(UiMsg::SetRepoDetails(text));
        });
    }

    fn install_repo_selected(self: &Rc<Self>) {
        let names: Vec<String> = {
            let ui = self.ui.borrow();
            ui.repo_visible_pkgs
                .iter()
                .filter(|p| {
                    ui.repo_selected.get(&p.name).copied().unwrap_or(false) && !p.installed
                })
                .map(|p| p.name.clone())
                .collect()
        };
        if names.is_empty() {
            self.set_status("Select one or more not-installed packages");
            return;
        }
        let mut args = vec!["pacman".into(), "-S".into(), "--noconfirm".into()];
        args.extend(names);
        self.execute_commands(
            "Install repository packages",
            vec![GuiCommand::new(
                "Install repository packages",
                true,
                args,
            )],
        );
    }

    fn remove_repo_selected(self: &Rc<Self>) {
        let names: Vec<String> = {
            let ui = self.ui.borrow();
            ui.repo_visible_pkgs
                .iter()
                .filter(|p| {
                    ui.repo_selected.get(&p.name).copied().unwrap_or(false) && p.installed
                })
                .map(|p| p.name.clone())
                .collect()
        };
        if names.is_empty() {
            self.set_status("Select one or more installed packages");
            return;
        }
        let mut args = vec!["pacman".into(), "-Rns".into(), "--noconfirm".into()];
        args.extend(names);
        self.execute_commands(
            "Remove repository packages",
            vec![GuiCommand::new(
                "Remove repository packages",
                true,
                args,
            )],
        );
    }
}

impl GuiApp {
    fn scan_images(&self) {
        self.set_busy(true);
        self.set_status("Scanning AppImage locations…");
        let core = self.core.clone();
        let tx = self.tx.clone();
        thread::spawn(move || {
            core.lock().unwrap().scan_app_images();
            let n = core.lock().unwrap().app_images.len();
            let _ = tx.send(UiMsg::PopulateImages);
            let _ = tx.send(UiMsg::SetBusy(false));
            let _ = tx.send(UiMsg::SetStatus(format!("Found {n} AppImages")));
        });
    }

    fn populate_images(&self) {
        while let Some(row) = self.image_list.row_at_index(0) {
            self.image_list.remove(&row);
        }
        let images = self.core.lock().unwrap().app_images.clone();
        for p in &images {
            let row = ListBoxRow::new();
            row.set_child(Some(&Label::new(Some(&format!("{}  —  {}", p.name, p.path)))));
            self.image_list.append(&row);
        }
    }

    fn show_image(&self) {
        let idx = self.ui.borrow().selected_image;
        let images = self.core.lock().unwrap().app_images.clone();
        if idx < 0 || (idx as usize) >= images.len() {
            return;
        }
        let p = &images[idx as usize];
        self.image_details.buffer().set_text(&format!(
            "Name: {}\nType: AppImage\nPath: {}",
            p.name, p.path
        ));
    }

    fn launch_image(&self) {
        let idx = self.ui.borrow().selected_image;
        let images = self.core.lock().unwrap().app_images.clone();
        if idx < 0 || (idx as usize) >= images.len() {
            self.set_status("Select an AppImage first");
            return;
        }
        let p = &images[idx as usize];
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(&p.path, fs::Permissions::from_mode(0o755));
        }
        match Command::new(&p.path).spawn() {
            Ok(_) => self.set_status("AppImage launched"),
            Err(e) => self.set_status(&format!("Launch failed: {e}")),
        }
    }

    fn remove_image(&self) {
        let idx = self.ui.borrow().selected_image;
        let path = {
            let images = self.core.lock().unwrap().app_images.clone();
            if idx < 0 || (idx as usize) >= images.len() {
                self.set_status("Select an AppImage first");
                return;
            }
            images[idx as usize].path.clone()
        };
        if fs::remove_file(&path).is_ok() {
            self.scan_images();
        } else {
            self.set_status("Could not remove AppImage");
        }
    }

    fn run_tool(self: &Rc<Self>, name: &str) {
        self.switch_page("logs");
        let mut args: Vec<String> = vec![];
        let mut priv_ = false;
        match name {
            "upgrade" => {
                args = vec!["pacman".into(), "-Syu".into(), "--noconfirm".into()];
                priv_ = true;
            }
            "sync" => {
                args = vec!["pacman".into(), "-Sy".into(), "--noconfirm".into()];
                priv_ = true;
            }
            "clean" => {
                args = vec!["pacman".into(), "-Sc".into(), "--noconfirm".into()];
                priv_ = true;
            }
            "deepclean" => {
                args = vec!["pacman".into(), "-Scc".into(), "--noconfirm".into()];
                priv_ = true;
            }
            "filedb" => {
                args = vec!["pacman".into(), "-Fy".into(), "--noconfirm".into()];
                priv_ = true;
            }
            "orphans" => {
                args = vec![
                    "bash".into(),
                    "-lc".into(),
                    "pacman -Qtdq | pacman -Rns --noconfirm -".into(),
                ];
                priv_ = true;
            }
            "aur-upgrade" => {
                let h = aur_helper();
                if !h.is_empty() {
                    args = vec![h, "-Syu".into(), "--noconfirm".into()];
                }
            }
            "verify" => args = vec!["pacman".into(), "-Qk".into()],
            "db-check" => args = vec!["pacman".into(), "-Dk".into()],
            "mirrors" => {
                args = vec![
                    "reflector".into(),
                    "--latest".into(),
                    "20".into(),
                    "--sort".into(),
                    "rate".into(),
                    "--save".into(),
                    "/etc/pacman.d/mirrorlist".into(),
                ];
                priv_ = true;
            }
            "keyring" => {
                args = vec!["pacman-key".into(), "--refresh-keys".into()];
                priv_ = true;
            }
            "paccache" => {
                args = vec!["paccache".into(), "-r".into(), "-k".into(), "2".into()];
                priv_ = true;
            }
            "daemon-reload" => {
                args = vec!["systemctl".into(), "daemon-reload".into()];
                priv_ = true;
            }
            "user-daemon-reload" => {
                args = vec!["systemctl".into(), "--user".into(), "daemon-reload".into()];
            }
            "mkinitcpio" => {
                args = vec!["mkinitcpio".into(), "-P".into()];
                priv_ = true;
            }
            "grub" => {
                args = vec![
                    "grub-mkconfig".into(),
                    "-o".into(),
                    "/boot/grub/grub.cfg".into(),
                ];
                priv_ = true;
            }
            "backup-db" => {
                args = vec![
                    "tar".into(),
                    "-cJf".into(),
                    "pacman-local-db.tar.xz".into(),
                    "-C".into(),
                    "/var/lib/pacman".into(),
                    "local".into(),
                ];
                priv_ = true;
            }
            "export-all" => {
                self.core
                    .lock()
                    .unwrap()
                    .export_command("pacman -Q", &["pacman", "-Q"]);
                self.refresh_logs();
                return;
            }
            "export-native" => {
                self.core
                    .lock()
                    .unwrap()
                    .export_command("pacman -Qne", &["pacman", "-Qne"]);
                self.refresh_logs();
                return;
            }
            "pacman-log" => {
                self.core.lock().unwrap().load_pacman_log();
                self.refresh_logs();
                return;
            }
            "checkupdates" => args = vec!["checkupdates".into()],
            "foreign" => args = vec!["pacman".into(), "-Qm".into()],
            "native-explicit" => args = vec!["pacman".into(), "-Qne".into()],
            "failed-services" => {
                args = vec![
                    "systemctl".into(),
                    "--failed".into(),
                    "--no-pager".into(),
                    "--no-legend".into(),
                ];
            }
            "journal-errors" => {
                args = vec![
                    "journalctl".into(),
                    "-p".into(),
                    "err".into(),
                    "-n".into(),
                    "120".into(),
                    "--no-pager".into(),
                ];
            }
            "pacnew" => {
                args = vec![
                    "bash".into(),
                    "-lc".into(),
                    "find /etc -name '*.pacnew' 2>/dev/null | sort".into(),
                ];
            }
            "ip" => args = vec!["ip".into(), "-br".into(), "a".into()],
            "uname" => args = vec!["uname".into(), "-a".into()],
            "df" => {
                args = vec![
                    "df".into(),
                    "-h".into(),
                    "/".into(),
                    "/var".into(),
                    "/home".into(),
                ];
            }
            "memory" => args = vec!["free".into(), "-h".into()],
            "pacdiff" => args = vec!["pacdiff".into(), "--safe".into()],
            "pkgfile" => {
                args = vec!["pkgfile".into(), "-u".into()];
                priv_ = true;
            }
            "flatpak-repair" => {
                args = vec!["flatpak".into(), "repair".into(), "--noninteractive".into()];
            }
            "flatpak-update" => args = vec!["flatpak".into(), "update".into(), "-y".into()],
            "flatpak-unused" => {
                args = vec![
                    "flatpak".into(),
                    "uninstall".into(),
                    "--unused".into(),
                    "-y".into(),
                ];
            }
            _ => return,
        }
        if !args.is_empty() {
            if name == "aur-upgrade" {
                self.execute_commands(
                    name,
                    vec![GuiCommand::new(name, false, args).with_auth()],
                );
                return;
            }
            self.execute_gui(name, priv_, args);
        }
    }

    fn refresh_logs(&self) {
        let text = self.core.lock().unwrap().log_lines.join("\n");
        self.log_view.buffer().set_text(&text);
    }

    fn load_news(&self) {
        self.set_status("Loading Arch Linux news…");
        let tx = self.tx.clone();
        thread::spawn(move || {
            let items = fetch_news(20);
            let mut b = String::new();
            for n in items {
                b.push_str(&format!(
                    "{}\n{}\n{}\n{}\n\n",
                    n.title,
                    n.date,
                    n.link,
                    truncate(&strip_html(&n.summary), 400)
                ));
            }
            if b.is_empty() {
                b.push_str("No news items available.");
            }
            let _ = tx.send(UiMsg::SetNews(b));
            let _ = tx.send(UiMsg::SetStatus("News loaded".into()));
        });
    }

    fn save_gui_settings(&self) {
        let checks = self.settings_checks.borrow();
        let mut app = self.core.lock().unwrap();
        for (key, c) in checks.iter() {
            match key.as_str() {
                "auto_check_updates" => app.settings.auto_check_updates = c.is_active(),
                "show_security_alerts" => app.settings.show_security_alerts = c.is_active(),
                "confirm_install" => app.settings.confirm_install = c.is_active(),
                "confirm_remove" => app.settings.confirm_remove = c.is_active(),
                "search_official" => app.settings.search_official = c.is_active(),
                "search_aur" => app.settings.search_aur = c.is_active(),
                "search_flatpak" => app.settings.search_flatpak = c.is_active(),
                "search_appimage" => app.settings.search_appimage = c.is_active(),
                _ => {}
            }
        }
        app.save_settings();
        drop(app);
        self.set_status("Settings saved");
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--version" || a == "-V") {
        println!("PKGER v{APP_VERSION}");
        return;
    }
    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!("{APP_NAME} — graphical package manager for Arch Linux");
        println!("Launch without arguments to open the PKGER workspace.");
        println!("Options: --version, -V, --help");
        return;
    }
    gui_main();
}
