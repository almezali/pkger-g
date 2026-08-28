// PKGER — single-file Go package manager for Arch Linux and Arch-based systems.
// The interface, labels, commands, storage layout, and feature set mirror pkger.py.
// It intentionally uses only the Go standard library so it can be built as one file.
//
// Build: go build -o pkger pkger.go
// Run:   ./pkger
// Help:  ./pkger --help

package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	gdk "github.com/diamondburned/gotk4/pkg/gdk/v4"
	"github.com/diamondburned/gotk4/pkg/gio/v2"
	"github.com/diamondburned/gotk4/pkg/glib/v2"
	gtk "github.com/diamondburned/gotk4/pkg/gtk/v4"
)

const (
	AppName    = "PKGER"
	AppVersion = "1.2.2"
	Developer  = "almezali"
	NewsRSS    = "https://archlinux.org/feeds/news/"
)

var (
	configDir     string
	configFile    string
	historyFile   string
	favoritesFile string
	debugEnabled  bool
	searchCache   = map[string]cachedSearch{}
	searchCacheMu sync.Mutex
	cacheTTL      = 45 * time.Second
)

type Settings struct {
	AutoCheckUpdates bool   `json:"auto_check_updates"`
	ShowSecurity     bool   `json:"show_security_alerts"`
	SearchOfficial   bool   `json:"search_official"`
	SearchAUR        bool   `json:"search_aur"`
	SearchFlatpak    bool   `json:"search_flatpak"`
	SearchAppImage   bool   `json:"search_appimage"`
	ThemeAccent      string `json:"theme_accent"`
	ConfirmInstall   bool   `json:"confirm_install"`
	ConfirmRemove    bool   `json:"confirm_remove"`
	MaxResults       int    `json:"max_search_results"`
	Language         string `json:"language"`
}

type Package struct {
	Name        string `json:"name"`
	Pkg         string `json:"pkg"`
	Version     string `json:"version"`
	Description string `json:"description"`
	Repository  string `json:"repository"`
	Installed   bool   `json:"installed"`
	Path        string `json:"path,omitempty"`
	Score       int    `json:"-"`
}

type Update struct {
	Name       string `json:"name"`
	From       string `json:"from"`
	To         string `json:"to"`
	Repository string `json:"repository,omitempty"`
	Source     string `json:"source"`
	Selected   bool   `json:"selected"`
}

type RepoPackage struct {
	Name      string `json:"name"`
	Version   string `json:"version"`
	Installed bool   `json:"installed"`
}

type HistoryEntry struct {
	Time  string `json:"time"`
	Label string `json:"label"`
	Cmd   string `json:"cmd"`
}

type NewsItem struct {
	Title   string
	Link    string
	Summary string
	Date    string
}

type SystemStats struct {
	Kernel     string
	Uptime     string
	Load       string
	DiskFree   string
	MemUsedPct string
	CacheSize  int64
}

type cachedSearch struct {
	At      time.Time
	Results []Package
}

type App struct {
	settings      Settings
	history       []HistoryEntry
	favorites     map[string]bool
	packages      []Package
	updates       []Update
	repos         map[string][]RepoPackage
	appImages     []Package
	stats         SystemStats
	held          []string
	page          string
	reader        *bufio.Reader
	logLines      []string
	lastResults   []Package
	selectedRepo  string
	selectedPkg   *Package
	selectedImage *Package
	running       bool
}

const (
	reset   = "\033[0m"
	bold    = "\033[1m"
	dim     = "\033[2m"
	cyan    = "\033[36m"
	blue    = "\033[34m"
	green   = "\033[32m"
	yellow  = "\033[33m"
	red     = "\033[31m"
	magenta = "\033[35m"
)

func main() {
	debugEnabled = strings.Contains(strings.ToLower(os.Getenv("PKGER_DEBUG")), "1") || strings.EqualFold(os.Getenv("PKGER_DEBUG"), "true")
	initPaths()
	version := flag.Bool("version", false, "Print version and exit")
	flag.BoolVar(version, "V", false, "Print version and exit")
	help := flag.Bool("help", false, "Show help")
	flag.Parse()
	if *version {
		fmt.Printf("PKGER v%s\n", AppVersion)
		return
	}
	if *help {
		printCLIHelp()
		return
	}
	if runtime.GOOS != "linux" {
		fmt.Fprintf(os.Stderr, "Warning: PKGER is designed for Arch Linux and Arch-based systems.\n")
	}
	guiMain()
}

func initPaths() {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	configDir = filepath.Join(home, ".config", "pkger")
	configFile = filepath.Join(configDir, "settings.json")
	historyFile = filepath.Join(configDir, "history.json")
	favoritesFile = filepath.Join(configDir, "favorites.json")
}

func defaultSettings() Settings {
	return Settings{AutoCheckUpdates: true, ShowSecurity: true, SearchOfficial: true, SearchAUR: true, SearchFlatpak: true, SearchAppImage: true, ThemeAccent: "default", ConfirmInstall: true, ConfirmRemove: true, MaxResults: 80, Language: "auto"}
}

func NewApp() *App {
	settings := defaultSettings()
	_ = readJSON(configFile, &settings)
	var history []HistoryEntry
	_ = readJSON(historyFile, &history)
	var favs []string
	_ = readJSON(favoritesFile, &favs)
	favoriteMap := map[string]bool{}
	for _, f := range favs {
		favoriteMap[f] = true
	}
	return &App{settings: settings, history: history, favorites: favoriteMap, page: "home", reader: bufio.NewReader(os.Stdin), repos: map[string][]RepoPackage{}, running: true}
}

func printCLIHelp() {
	fmt.Println(AppName + " — graphical-style terminal package manager for Arch Linux")
	fmt.Println("Launch without arguments to open the PKGER workspace.")
	fmt.Println("Options: --version, -V, --help")
}

func (a *App) Run() {
	a.loadInitialData()
	for a.running {
		a.draw()
		fmt.Printf("\n%sPKGER command%s (type 'help' for commands): ", cyan, reset)
		line, err := a.reader.ReadString('\n')
		if err != nil && !errors.Is(err, io.EOF) {
			a.appendLog("Input error: " + err.Error())
			break
		}
		cmd := strings.TrimSpace(line)
		if cmd == "" {
			continue
		}
		a.handleCommand(cmd)
	}
	fmt.Println("Goodbye.")
}

func (a *App) draw() {
	fmt.Print("\033[H\033[2J")
	osInfo := detectDistro()
	fmt.Printf("%s%s PKGER v%s%s  %s— %s%s\n", bold, cyan, AppVersion, reset, dim, osInfo, reset)
	fmt.Println(strings.Repeat("─", 92))
	fmt.Printf("%s%-16s%s %-12s %-12s %-12s\n", blue, "MAIN", reset, "PACKAGES", "SYSTEM", "MORE")
	fmt.Printf("  %s[home]%s Home     %s[install]%s Install     %s[updates]%s Updates     %s[repos]%s Sources\n", active(a.page == "home"), reset, active(a.page == "install"), reset, active(a.page == "updates"), reset, active(a.page == "repos"), reset)
	fmt.Printf("  %s[images]%s AppImages %s[tools]%s Tools       %s[settings]%s Settings   %s[logs]%s Logs       %s[news]%s News\n", active(a.page == "images"), reset, active(a.page == "tools"), reset, active(a.page == "settings"), reset, active(a.page == "logs"), reset, active(a.page == "news"), reset)
	fmt.Println(strings.Repeat("─", 92))
	switch a.page {
	case "home":
		a.drawHome()
	case "install":
		a.drawInstall()
	case "updates":
		a.drawUpdates()
	case "repos":
		a.drawRepos()
	case "images":
		a.drawImages()
	case "tools":
		a.drawTools()
	case "settings":
		a.drawSettings()
	case "logs":
		a.drawLogs()
	case "news":
		a.drawNews()
	default:
		a.drawHome()
	}
}

func active(on bool) string {
	if on {
		return green + bold
	}
	return dim
}
func detectDistro() string {
	b, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return "Arch Linux"
	}
	name, version := "Arch Linux", ""
	for _, line := range strings.Split(string(b), "\n") {
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		v := strings.Trim(parts[1], "\"")
		if parts[0] == "NAME" {
			name = v
		}
		if parts[0] == "VERSION_ID" {
			version = v
		}
	}
	if version != "" {
		return name + " " + version
	}
	return name
}

func (a *App) drawHome() {
	score := healthScore(len(a.packages), a.updates, a.held, a.stats)
	sec := 0
	for _, u := range a.updates {
		if severity(u.Name, u.Repository, u.Source) == "Security" {
			sec++
		}
	}
	fmt.Printf("%sWelcome to %s v%s%s\n", bold, AppName, AppVersion, reset)
	fmt.Printf("%s%s — Intelligent Package Management%s\n\n", dim, detectDistro(), reset)
	fmt.Printf("%s┌────────────┬────────────┬────────────┬────────────┬────────────┬────────────┐%s\n", cyan, reset)
	fmt.Printf("%s│ %-10s │ %-10s │ %-10s │ %-10s │ %-10s │ %-10s │%s\n", cyan, "HEALTH", "INSTALLED", "UPDATES", "SECURITY", "CACHE", "KERNEL", reset)
	fmt.Printf("%s│ %-10s │ %-10d │ %-10d │ %-10d │ %-10s │ %-10s │%s\n", cyan, healthColor(score), len(a.packages), len(a.updates), sec, humanSize(a.stats.CacheSize), truncate(a.stats.Kernel, 10), reset)
	fmt.Printf("%s└────────────┴────────────┴────────────┴────────────┴────────────┴────────────┘%s\n\n", cyan, reset)
	fmt.Printf("%sQuick actions:%s  updates  |  search <query>  |  local <path>  |  repos  |  tools\n", bold, reset)
	fmt.Printf("%sSystem:%s Uptime %s  ·  Load %s  ·  Disk free %s  ·  RAM used %s\n", dim, reset, a.stats.Uptime, a.stats.Load, a.stats.DiskFree, a.stats.MemUsedPct)
}
func healthColor(score int) string {
	c := green
	if score < 70 {
		c = yellow
	}
	if score < 50 {
		c = red
	}
	return c + fmt.Sprintf("%d%%%s", score, reset)
}
func (a *App) drawInstall() {
	fmt.Printf("%sInstall & Search%s\n", bold, reset)
	fmt.Println("Sources: unified, official, aur, installed, installed_native, installed_foreign, installed_flatpak, installed_appimage, flatpak, appimage, developer")
	fmt.Printf("Results: %d\n", len(a.lastResults))
	if len(a.lastResults) == 0 {
		fmt.Println(dim + "Use: search <query> [source]  or  installed" + reset)
		return
	}
	for i, p := range a.lastResults {
		if i >= a.settings.MaxResults {
			break
		}
		marker := " "
		if p.Installed {
			marker = green + "✓" + reset
		}
		fmt.Printf("%3d %s %-32s %-18s %-12s %s\n", i+1, marker, truncate(p.Name, 32), truncate(p.Version, 18), p.Repository, truncate(p.Description, 48))
	}
	fmt.Println(dim + "Use: select <number>, install, remove, details, favorite <number>, export" + reset)
}
func (a *App) drawUpdates() {
	fmt.Printf("%sUpdates%s  %s\n", bold, reset, formatUpdateSummary(a.updates))
	if len(a.updates) == 0 {
		fmt.Println(green + "System is up to date." + reset)
		return
	}
	for i, u := range a.updates {
		mark := " "
		if u.Selected {
			mark = green + "✓" + reset
		}
		fmt.Printf("%3d %s %-30s %-18s → %-18s %-10s\n", i+1, mark, u.Name, u.From, u.To, severity(u.Name, u.Repository, u.Source))
	}
	fmt.Println(dim + "Use: update-select all|none|security|<number>, apply, upgrade" + reset)
}
func (a *App) drawRepos() {
	fmt.Printf("%sSources / Repositories%s\n", bold, reset)
	if len(a.repos) == 0 {
		fmt.Println(dim + "No repository data. Use: refresh repos" + reset)
		return
	}
	keys := make([]string, 0, len(a.repos))
	for k := range a.repos {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for i, k := range keys {
		kind := classifyRepo(k)
		fmt.Printf("%3d %-20s %-13s %d packages\n", i+1, k, kind, len(a.repos[k]))
	}
	if a.selectedRepo != "" {
		fmt.Printf("\n%sSelected:%s %s\n", bold, reset, a.selectedRepo)
		for i, p := range a.repos[a.selectedRepo] {
			if i >= 40 {
				fmt.Println(dim + "… truncated" + reset)
				break
			}
			mark := " "
			if p.Installed {
				mark = green + "✓" + reset
			}
			fmt.Printf(" %s %-32s %s\n", mark, p.Name, p.Version)
		}
	}
	fmt.Println(dim + "Use: repo-select <name>, repo-search <query>, repo-install <pkg>, repo-remove <pkg>, repo-export" + reset)
}
func (a *App) drawImages() {
	fmt.Printf("%sAppImages%s  %d found\n", bold, reset, len(a.appImages))
	for i, p := range a.appImages {
		fmt.Printf("%3d %-30s %s\n", i+1, truncate(p.Name, 30), p.Path)
	}
	fmt.Println(dim + "Use: scan, image-select <number>, image-import <path>, image-launch, image-remove" + reset)
}
func (a *App) drawTools() {
	fmt.Printf("%sSystem maintenance, diagnostics, and logs%s\n\n", bold, reset)
	fmt.Println("Maintenance: upgrade | sync | clean | deepclean | filedb | orphans | aur-upgrade | verify | db-check | mirrors | keyring | paccache | daemon-reload | user-daemon-reload")
	fmt.Println("Diagnostics: checkupdates | foreign | native-explicit | failed-services | journal-errors | pacnew | ip | uname | df | memory | pacdiff | pkgfile")
	fmt.Println("Optional: flatpak-version | flatpak-repair | flatpak-update | flatpak-unused | fwupdmgr-devices | fwupdmgr-updates")
	fmt.Println("Advanced: mkinitcpio | grub | backup-db | export-all | export-native | file-search <path> | file-owner <path> | pacman-log")
}
func (a *App) drawSettings() {
	fmt.Printf("%sSettings%s\n", bold, reset)
	fmt.Printf("1. Auto-check updates on startup: %v\n2. Show security alerts prominently: %v\n3. Confirm before installing: %v\n4. Confirm before removing: %v\n", a.settings.AutoCheckUpdates, a.settings.ShowSecurity, a.settings.ConfirmInstall, a.settings.ConfirmRemove)
	fmt.Printf("5. Search official: %v\n6. Search AUR: %v\n7. Search Flatpak: %v\n8. Search AppImage: %v\n9. Max results: %d\n", a.settings.SearchOfficial, a.settings.SearchAUR, a.settings.SearchFlatpak, a.settings.SearchAppImage, a.settings.MaxResults)
	fmt.Println(dim + "Use: set <key> <value>, save-settings" + reset)
}
func (a *App) drawLogs() {
	fmt.Printf("%sLogs%s\n", bold, reset)
	if len(a.logLines) == 0 {
		fmt.Println(dim + "No output yet." + reset)
	}
	start := 0
	if len(a.logLines) > 80 {
		start = len(a.logLines) - 80
	}
	for _, l := range a.logLines[start:] {
		fmt.Println(l)
	}
	fmt.Println(dim + "Use: clear-log, export-log <path>, history, pacman-log" + reset)
}
func (a *App) drawNews() {
	fmt.Printf("%sArch Linux News%s\n", bold, reset)
	items := fetchNews(15)
	if len(items) == 0 {
		fmt.Println("No news items available.")
		return
	}
	for _, n := range items {
		fmt.Printf("%s%s%s\n  %s\n  %s\n  %s\n\n", bold, n.Title, reset, n.Date, n.Link, truncate(stripHTML(n.Summary), 320))
	}
}

func (a *App) handleCommand(line string) {
	parts := strings.Fields(line)
	if len(parts) == 0 {
		return
	}
	cmd := strings.ToLower(parts[0])
	args := parts[1:]
	switch cmd {
	case "help", "?":
		a.help()
	case "quit", "exit", "q":
		a.running = false
	case "home", "dashboard":
		a.page = "home"
	case "install":
		a.page = "install"
		if len(args) > 0 {
			a.search(strings.Join(args, " "))
		}
	case "search":
		a.page = "install"
		a.search(strings.Join(args, " "))
	case "updates":
		a.page = "updates"
		a.loadUpdates(false)
	case "repos", "sources":
		a.page = "repos"
		if len(a.repos) == 0 {
			a.loadRepos()
		}
	case "images", "appimages":
		a.page = "images"
		a.scanAppImages()
	case "tools", "system":
		a.page = "tools"
	case "settings":
		a.page = "settings"
	case "logs", "log":
		a.page = "logs"
	case "news":
		a.page = "news"
	case "refresh":
		a.refresh(args)
	case "installed":
		a.page = "install"
		a.search("installed")
	case "select":
		a.selectResult(args)
	case "details":
		a.showDetails()
	case "install-pkg", "install-package":
		a.packageAction("install")
	case "remove", "remove-pkg":
		a.packageAction("remove")
	case "favorite":
		a.toggleFavorite(args)
	case "export":
		a.exportVisible(args)
	case "update-select":
		a.selectUpdates(args)
	case "apply":
		a.applySelected()
	case "upgrade":
		a.runSystemAction("upgrade")
	case "repo-select":
		a.selectRepo(args)
	case "repo-search":
		a.repoSearch(args)
	case "repo-install":
		a.repoPackageAction(args, "install")
	case "repo-remove":
		a.repoPackageAction(args, "remove")
	case "repo-export":
		a.exportRepo()
	case "scan":
		a.scanAppImages()
	case "image-select":
		a.selectImage(args)
	case "image-import":
		if len(args) > 0 {
			a.importAppImage(strings.Join(args, " "))
		}
	case "image-launch":
		a.launchAppImage()
	case "image-remove":
		a.removeAppImage()
	case "set":
		a.setSetting(args)
	case "save-settings":
		a.saveSettings()
	case "clear-log":
		a.logLines = nil
	case "export-log":
		a.exportLog(args)
	case "history":
		a.showHistory()
	case "pacman-log":
		a.loadPacmanLog()
	default:
		a.runTool(cmd, args)
	}
}

func (a *App) help() {
	fmt.Println("\nNavigation: home, install, updates, repos, images, tools, settings, logs, news")
	fmt.Println("Search: search <query> [unified|official|aur|installed|flatpak|appimage|developer]")
	fmt.Println("Packages: select <n>, details, install-pkg, remove, favorite <n>, export [path]")
	fmt.Println("Updates: update-select all|none|security|<n>, apply, upgrade")
	fmt.Println("Repositories: repo-select <name>, repo-search <query>, repo-install <pkg>, repo-remove <pkg>, repo-export")
	fmt.Println("AppImages: scan, image-select <n>, image-import <path>, image-launch, image-remove")
	fmt.Println("Tools: upgrade, sync, clean, deepclean, filedb, orphans, aur-upgrade, verify, db-check, mirrors, keyring, paccache, daemon-reload, user-daemon-reload, mkinitcpio, grub, backup-db, export-all, export-native, file-search <path>, file-owner <path>, pacman-log")
	fmt.Println("Other: refresh [all|updates|repos|images], set <key> <value>, save-settings, history, clear-log, export-log <path>, quit")
}

func (a *App) loadInitialData() {
	fmt.Println("Initializing PKGER…")
	a.stats = systemStats()
	a.loadInstalled()
	a.scanAppImages()
	a.loadRepos()
	if a.settings.AutoCheckUpdates {
		a.loadUpdates(false)
	}
}
func (a *App) refresh(args []string) {
	what := "all"
	if len(args) > 0 {
		what = args[0]
	}
	switch what {
	case "all":
		a.loadInstalled()
		a.loadUpdates(false)
		a.loadRepos()
		a.scanAppImages()
	case "updates":
		a.loadUpdates(false)
	case "repos":
		a.loadRepos()
	case "images":
		a.scanAppImages()
	}
	a.stats = systemStats()
}

func commandExists(name string) bool { _, err := exec.LookPath(name); return err == nil }
func runCapture(timeout time.Duration, name string, args ...string) (string, string, int) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	c := exec.CommandContext(ctx, name, args...)
	out, err := c.Output()
	if err == nil {
		return string(out), "", 0
	}
	code := 1
	var ee *exec.ExitError
	if errors.As(err, &ee) {
		code = ee.ExitCode()
	}
	return string(out), string(eeStderr(err)), code
}
func eeStderr(err error) string {
	if ee, ok := err.(*exec.ExitError); ok {
		return string(ee.Stderr)
	}
	return err.Error()
}

func (a *App) loadInstalled() {
	out, _, code := runCapture(60*time.Second, "pacman", "-Q")
	if code != 0 {
		a.appendLog("pacman -Q unavailable or failed")
		return
	}
	var list []Package
	for _, line := range strings.Split(out, "\n") {
		p := strings.Fields(line)
		if len(p) >= 2 {
			list = append(list, Package{Name: p[0], Pkg: p[0], Version: p[1], Repository: "installed", Description: "Installed package", Installed: true})
		}
	}
	a.packages = list
	a.lastResults = list
	a.appendLog(fmt.Sprintf("Loaded %d installed packages", len(list)))
}
func (a *App) loadRepos() {
	out, _, code := runCapture(75*time.Second, "pacman", "-Sl")
	if code != 0 {
		a.appendLog("pacman -Sl failed")
		return
	}
	m := map[string][]RepoPackage{}
	for _, line := range strings.Split(out, "\n") {
		p := strings.Fields(line)
		if len(p) >= 2 {
			v := "-"
			if len(p) > 2 {
				v = p[2]
			}
			m[p[0]] = append(m[p[0]], RepoPackage{Name: p[1], Version: v, Installed: strings.Contains(line, "[installed]")})
		}
	}
	a.repos = m
	a.appendLog(fmt.Sprintf("Loaded %d repositories", len(m)))
}
func (a *App) loadUpdates(syncDB bool) {
	if syncDB {
		a.runTool("sync", nil)
	}
	out, _, code := runCapture(55*time.Second, "pacman", "-Qu")
	var list []Update
	if code == 0 {
		for _, line := range strings.Split(out, "\n") {
			p := strings.Fields(line)
			if len(p) >= 4 && p[2] == "->" {
				list = append(list, Update{Name: p[0], From: p[1], To: p[3], Source: "pacman"})
			}
		}
	}
	if commandExists("flatpak") {
		f, _, c := runCapture(45*time.Second, "flatpak", "remote-ls", "--updates", "--columns=application,version", "flathub")
		if c == 0 {
			for _, line := range strings.Split(f, "\n") {
				p := strings.Fields(line)
				if len(p) > 0 {
					v := "update"
					if len(p) > 1 {
						v = p[1]
					}
					list = append(list, Update{Name: p[0], From: "-", To: v, Source: "flatpak"})
				}
			}
		}
	}
	a.updates = list
	a.appendLog(fmt.Sprintf("Loaded %d updates", len(list)))
}

func (a *App) search(text string) {
	query := strings.TrimSpace(text)
	mode := "unified"
	fields := strings.Fields(query)
	if len(fields) > 1 {
		candidate := strings.ToLower(fields[len(fields)-1])
		if isSearchMode(candidate) {
			mode = candidate
			query = strings.TrimSpace(strings.TrimSuffix(query, fields[len(fields)-1]))
		}
	}
	if query == "installed" {
		mode = "installed"
		query = ""
	}
	if query == "" && mode != "installed" && mode != "appimage" {
		fmt.Println("Enter a query or use installed.")
		return
	}
	key := mode + "|" + strings.ToLower(query)
	searchCacheMu.Lock()
	c, ok := searchCache[key]
	if ok && time.Since(c.At) < cacheTTL {
		a.lastResults = append([]Package(nil), c.Results...)
		searchCacheMu.Unlock()
		return
	}
	searchCacheMu.Unlock()
	var wg sync.WaitGroup
	ch := make(chan []Package, 5)
	jobs := 0
	add := func(fn func() []Package) { jobs++; wg.Add(1); go func() { defer wg.Done(); ch <- fn() }() }
	if mode == "official" || mode == "unified" {
		add(func() []Package { return a.searchPacman(query) })
	}
	if mode == "installed" {
		add(func() []Package { return a.searchInstalled(query) })
	}
	if mode == "aur" || mode == "unified" {
		add(func() []Package { return a.searchAUR(query) })
	}
	if mode == "flatpak" || mode == "unified" {
		add(func() []Package { return a.searchFlatpak(query) })
	}
	if mode == "appimage" || mode == "unified" {
		add(func() []Package { return scanAppImages(query) })
	}
	if mode == "developer" {
		add(func() []Package { return a.searchDeveloper(query) })
	}
	go func() { wg.Wait(); close(ch) }()
	var merged []Package
	seen := map[string]bool{}
	for batch := range ch {
		for _, p := range batch {
			k := p.Repository + "/" + p.Pkg
			if !seen[k] {
				seen[k] = true
				merged = append(merged, p)
			}
		}
	}
	_ = jobs
	q := strings.ToLower(query)
	for i := range merged {
		hay := strings.ToLower(merged[i].Name + " " + merged[i].Description + " " + merged[i].Repository)
		if q != "" {
			score := 0
			if strings.Contains(hay, q) {
				score += 50
			}
			if fuzzy(q, strings.ToLower(merged[i].Name)) {
				score += 25
			}
			for _, t := range strings.Fields(q) {
				if strings.Contains(strings.ToLower(merged[i].Name), t) {
					score += 12
				} else if strings.Contains(strings.ToLower(merged[i].Description), t) {
					score += 8
				}
			}
			if score == 0 {
				continue
			}
			if merged[i].Installed {
				score++
			}
			merged[i].Score = score
		}
	}
	sort.SliceStable(merged, func(i, j int) bool {
		if merged[i].Score != merged[j].Score {
			return merged[i].Score > merged[j].Score
		}
		return strings.ToLower(merged[i].Name) < strings.ToLower(merged[j].Name)
	})
	for i := range merged {
		merged[i].Score = 0
	}
	searchCacheMu.Lock()
	searchCache[key] = cachedSearch{time.Now(), append([]Package(nil), merged...)}
	searchCacheMu.Unlock()
	a.lastResults = merged
	a.appendLog(fmt.Sprintf("Search %q in %s returned %d packages", query, mode, len(merged)))
}
func isSearchMode(s string) bool {
	switch s {
	case "unified", "official", "aur", "installed", "flatpak", "appimage", "developer":
		return true
	}
	return false
}
func (a *App) searchPacman(q string) []Package {
	if q == "" {
		return nil
	}
	out, _, code := runCapture(40*time.Second, "pacman", "-Ss", q)
	if code != 0 {
		return nil
	}
	return parsePacman(out, "official", a.installedSet())
}
func (a *App) searchInstalled(q string) []Package {
	args := []string{"-Q"}
	if q != "" {
		args = []string{"-Qs", q}
	}
	out, _, code := runCapture(40*time.Second, "pacman", args...)
	if code != 0 {
		return nil
	}
	return parsePacman(out, "installed", a.installedSet())
}
func (a *App) searchAUR(q string) []Package {
	h := aurHelper()
	if h == "" || q == "" {
		return nil
	}
	out, _, code := runCapture(50*time.Second, h, "-Ss", q)
	if code != 0 {
		return nil
	}
	return parseAUR(out, a.installedSet())
}
func (a *App) searchFlatpak(q string) []Package {
	if !commandExists("flatpak") || q == "" {
		return nil
	}
	out, _, code := runCapture(50*time.Second, "flatpak", "search", "--columns=name,application,description", q)
	if code != 0 {
		return nil
	}
	var r []Package
	for _, line := range strings.Split(out, "\n") {
		p := strings.Split(line, "\t")
		if len(p) > 0 && strings.TrimSpace(p[0]) != "" {
			id := strings.TrimSpace(p[0])
			if len(p) > 1 {
				id = strings.TrimSpace(p[1])
			}
			d := "Flatpak application"
			if len(p) > 2 {
				d = strings.TrimSpace(p[2])
			}
			r = append(r, Package{Name: id, Pkg: id, Description: d, Repository: "flatpak"})
		}
	}
	return r
}
func (a *App) searchDeveloper(q string) []Package {
	if q == "" {
		return nil
	}
	return a.searchPacman(q)
}
func parsePacman(out, source string, installed map[string]bool) []Package {
	var r []Package
	lines := strings.Split(out, "\n")
	for i := 0; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, " ") {
			continue
		}
		p := strings.SplitN(line, " ", 2)
		if len(p) < 2 {
			continue
		}
		full, ver := p[0], p[1]
		repo, name := source, full
		if j := strings.Index(full, "/"); j >= 0 {
			repo = full[:j]
			name = full[j+1:]
		}
		desc := ""
		if i+1 < len(lines) && strings.HasPrefix(lines[i+1], "    ") {
			desc = strings.TrimSpace(lines[i+1])
			i++
		}
		r = append(r, Package{Name: name, Pkg: name, Version: ver, Description: desc, Repository: repo, Installed: installed[name]})
	}
	return r
}
func parseAUR(out string, installed map[string]bool) []Package {
	var r []Package
	for _, line := range strings.Split(out, "\n") {
		if line == "" || strings.HasPrefix(line, " ") {
			continue
		}
		p := strings.SplitN(line, " ", 3)
		if len(p) >= 2 {
			name := p[0]
			ver := p[1]
			if strings.HasPrefix(name, "aur/") {
				name = strings.TrimPrefix(name, "aur/")
			}
			d := ""
			if len(p) > 2 {
				d = p[2]
			}
			r = append(r, Package{Name: name, Pkg: name, Version: ver, Description: d, Repository: "aur", Installed: installed[name]})
		}
	}
	return r
}
func (a *App) installedSet() map[string]bool {
	m := map[string]bool{}
	for _, p := range a.packages {
		m[p.Name] = true
	}
	return m
}
func fuzzy(pattern, text string) bool {
	if pattern == "" {
		return true
	}
	j := 0
	for _, r := range strings.ToLower(text) {
		if j < len(pattern) && rune(pattern[j]) == r {
			j++
		}
	}
	return j == len(pattern)
}

func (a *App) selectResult(args []string) {
	if len(args) == 0 {
		return
	}
	n, _ := strconv.Atoi(args[0])
	if n < 1 || n > len(a.lastResults) {
		fmt.Println("Invalid result number.")
		return
	}
	p := a.lastResults[n-1]
	a.selectedPkg = &p
	a.appendLog("Selected " + p.Name)
}
func (a *App) showDetails() {
	if a.selectedPkg == nil {
		fmt.Println("Select a package first.")
		return
	}
	p := a.selectedPkg
	fmt.Printf("\n%sName:%s %s\nRepository: %s\nVersion: %s\nInstalled: %v\nDescription: %s\n", bold, reset, p.Name, p.Repository, p.Version, p.Installed, p.Description)
	var out string
	if p.Repository == "flatpak" {
		out, _, _ = runCapture(35*time.Second, "flatpak", "info", p.Name)
	} else if p.Repository == "aur" {
		if h := aurHelper(); h != "" {
			out, _, _ = runCapture(35*time.Second, h, "-Si", p.Name)
		}
	} else {
		out, _, _ = runCapture(35*time.Second, "pacman", "-Si", p.Name)
		if out == "" {
			out, _, _ = runCapture(25*time.Second, "pacman", "-Qi", p.Name)
		}
	}
	if out != "" {
		fmt.Println(out)
	}
}
func (a *App) packageAction(action string) {
	if a.selectedPkg == nil {
		fmt.Println("Select a package first.")
		return
	}
	p := a.selectedPkg
	if action == "remove" && a.settings.ConfirmRemove && !a.confirm("Remove "+p.Name+"? ") {
		return
	}
	if action == "install" && !p.Installed && a.settings.ConfirmInstall && !a.confirm("Install "+p.Name+"? ") {
		return
	}
	var cmd []string
	priv := false
	if action == "remove" {
		if p.Repository == "flatpak" {
			cmd = []string{"flatpak", "uninstall", "-y", p.Name}
		} else {
			cmd = []string{"pacman", "-Rns", "--noconfirm", p.Name}
			priv = true
		}
	} else if p.Repository == "flatpak" {
		cmd = []string{"flatpak", "install", "-y", p.Name}
	} else if p.Repository == "aur" {
		h := aurHelper()
		if h == "" {
			fmt.Println("No AUR helper installed (yay or paru).")
			return
		}
		cmd = []string{h, "-S", "--noconfirm", p.Name}
	} else {
		cmd = []string{"pacman", "-S", "--noconfirm", p.Name}
		priv = true
	}
	a.runCommand(a.actionLabel(action, p.Name), priv, cmd...)
	a.loadInstalled()
}
func (a *App) actionLabel(action, name string) string { return action + " " + name }

func (a *App) selectUpdates(args []string) {
	if len(args) == 0 {
		return
	}
	what := strings.ToLower(args[0])
	for i := range a.updates {
		a.updates[i].Selected = false
	}
	if what == "all" {
		for i := range a.updates {
			a.updates[i].Selected = true
		}
	} else if what == "security" {
		for i := range a.updates {
			a.updates[i].Selected = severity(a.updates[i].Name, a.updates[i].Repository, a.updates[i].Source) == "Security"
		}
	} else if n, e := strconv.Atoi(what); e == nil && n > 0 && n <= len(a.updates) {
		a.updates[n-1].Selected = true
	}
}
func (a *App) applySelected() {
	var pac, flat []string
	for _, u := range a.updates {
		if u.Selected {
			if u.Source == "flatpak" {
				flat = append(flat, u.Name)
			} else {
				pac = append(pac, u.Name)
			}
		}
	}
	if len(pac) > 0 {
		args := append([]string{"pacman", "-S", "--noconfirm"}, pac...)
		a.runCommand("apply selected pacman updates", true, args...)
	}
	if len(flat) > 0 {
		args := append([]string{"flatpak", "update", "-y"}, flat...)
		a.runCommand("apply selected flatpak updates", false, args...)
	}
	a.loadUpdates(false)
}
func formatUpdateSummary(u []Update) string {
	sec := 0
	fp := 0
	for _, x := range u {
		if severity(x.Name, x.Repository, x.Source) == "Security" {
			sec++
		}
		if x.Source == "flatpak" {
			fp++
		}
	}
	return fmt.Sprintf("Pending: %d total | pacman: %d | Flatpak: %d | Security-related: %d", len(u), len(u)-fp, fp, sec)
}
func severity(name, repo, source string) string {
	n := strings.ToLower(name)
	for _, x := range []string{"linux", "kernel"} {
		if strings.Contains(n, x) {
			return "Kernel"
		}
	}
	for _, x := range []string{"openssl", "openssh", "sudo", "polkit", "glibc", "systemd"} {
		if strings.Contains(n, x) {
			return "Security"
		}
	}
	if strings.EqualFold(source, "flatpak") || strings.EqualFold(repo, "flatpak") {
		return "Important"
	}
	return "Update"
}
func healthScore(installed int, updates []Update, held []string, st SystemStats) int {
	score := 100
	sec, kernel := 0, 0
	for _, u := range updates {
		switch severity(u.Name, u.Repository, u.Source) {
		case "Security":
			sec++
		case "Kernel":
			kernel++
		}
	}
	if sec > 0 {
		score -= min(30, sec*8)
	}
	if kernel > 0 {
		score -= min(15, kernel*5)
	}
	if len(updates) > 50 {
		score -= 10
	} else if len(updates) > 20 {
		score -= 5
	}
	score -= min(10, len(held)*2)
	gb := float64(st.CacheSize) / (1024 * 1024 * 1024)
	if gb > 2 {
		score -= 10
	} else if gb > 1 {
		score -= 5
	}
	return max(0, min(100, score))
}

func (a *App) selectRepo(args []string) {
	if len(args) == 0 {
		return
	}
	name := strings.Join(args, " ")
	if _, ok := a.repos[name]; !ok {
		fmt.Println("Repository not found.")
		return
	}
	a.selectedRepo = name
}
func (a *App) repoSearch(args []string) {
	if a.selectedRepo == "" {
		fmt.Println("Select a repository first.")
		return
	}
	q := strings.ToLower(strings.Join(args, " "))
	var r []RepoPackage
	for _, p := range a.repos[a.selectedRepo] {
		if q == "" || strings.Contains(strings.ToLower(p.Name), q) {
			r = append(r, p)
		}
	}
	a.repos[a.selectedRepo] = r
}
func (a *App) repoPackageAction(args []string, action string) {
	if a.selectedRepo == "" || len(args) == 0 {
		fmt.Println("Select a repository and package first.")
		return
	}
	name := args[0]
	if action == "install" {
		a.runCommand("install "+name, true, "pacman", "-S", "--noconfirm", name)
	} else {
		a.runCommand("remove "+name, true, "pacman", "-Rns", "--noconfirm", name)
	}
	a.loadInstalled()
}
func (a *App) exportRepo() {
	if a.selectedRepo == "" {
		fmt.Println("Select a repository first.")
		return
	}
	path := "pkger-" + a.selectedRepo + "-packages.txt"
	var b strings.Builder
	for _, p := range a.repos[a.selectedRepo] {
		b.WriteString(p.Name + "\n")
	}
	if os.WriteFile(path, []byte(b.String()), 0644) == nil {
		a.appendLog("Exported repository list to " + path)
	}
}

func scanAppImages(query string) []Package {
	home, _ := os.UserHomeDir()
	locations := []string{filepath.Join(home, "Applications"), filepath.Join(home, ".local", "bin"), "/opt"}
	q := strings.ToLower(query)
	var out []Package
	for _, loc := range locations {
		filepath.Walk(loc, func(path string, info os.FileInfo, err error) error {
			if err != nil || info == nil {
				return nil
			}
			if info.IsDir() {
				return nil
			}
			if strings.EqualFold(filepath.Ext(path), ".AppImage") || strings.EqualFold(filepath.Ext(path), ".appimage") {
				name := strings.TrimSuffix(info.Name(), filepath.Ext(info.Name()))
				if q == "" || strings.Contains(strings.ToLower(name), q) {
					out = append(out, Package{Name: name, Pkg: name, Description: "Local AppImage: " + path, Repository: "appimage", Installed: true, Path: path})
				}
			}
			return nil
		})
	}
	return out
}
func (a *App) scanAppImages() {
	a.appImages = scanAppImages("")
	a.appendLog(fmt.Sprintf("Found %d AppImages", len(a.appImages)))
}
func (a *App) selectImage(args []string) {
	if len(args) == 0 {
		return
	}
	n, _ := strconv.Atoi(args[0])
	if n < 1 || n > len(a.appImages) {
		fmt.Println("Invalid image number.")
		return
	}
	p := a.appImages[n-1]
	a.selectedImage = &p
}
func (a *App) importAppImage(src string) {
	home, _ := os.UserHomeDir()
	dstDir := filepath.Join(home, "Applications")
	_ = os.MkdirAll(dstDir, 0755)
	dst := filepath.Join(dstDir, filepath.Base(src))
	in, err := os.Open(src)
	if err != nil {
		fmt.Println("Import failed:", err)
		return
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		fmt.Println("Import failed:", err)
		return
	}
	_, err = io.Copy(out, in)
	out.Close()
	if err == nil {
		_ = os.Chmod(dst, 0755)
		a.appendLog("Imported AppImage: " + dst)
		a.scanAppImages()
	}
}
func (a *App) launchAppImage() {
	if a.selectedImage == nil {
		fmt.Println("Select an AppImage first.")
		return
	}
	_ = os.Chmod(a.selectedImage.Path, 0755)
	c := exec.Command(a.selectedImage.Path)
	c.Stdout = os.Stdout
	c.Stderr = os.Stderr
	if err := c.Start(); err != nil {
		fmt.Println("Launch failed:", err)
	}
}
func (a *App) removeAppImage() {
	if a.selectedImage == nil {
		fmt.Println("Select an AppImage first.")
		return
	}
	if a.confirm("Delete " + a.selectedImage.Path + "? ") {
		if os.Remove(a.selectedImage.Path) == nil {
			a.appendLog("Removed AppImage: " + a.selectedImage.Path)
			a.scanAppImages()
		}
	}
}

func (a *App) runTool(name string, args []string) {
	switch name {
	case "upgrade":
		a.confirmRun("Full system upgrade", true, "pacman", "-Syu", "--noconfirm")
	case "sync":
		a.confirmRun("Sync databases", true, "pacman", "-Sy", "--noconfirm")
	case "clean":
		a.confirmRun("Clean package cache", true, "pacman", "-Sc", "--noconfirm")
	case "deepclean":
		a.confirmRun("Remove all cached packages", true, "pacman", "-Scc", "--noconfirm")
	case "filedb":
		a.confirmRun("Sync file databases", true, "pacman", "-Fy", "--noconfirm")
	case "orphans":
		a.confirmRun("Remove orphan packages", true, "bash", "-lc", "pacman -Qtdq | pacman -Rns --noconfirm -")
	case "aur-upgrade":
		if h := aurHelper(); h != "" {
			a.confirmRun("Full AUR upgrade", false, h, "-Syu", "--noconfirm")
		} else {
			fmt.Println("No AUR helper installed.")
		}
	case "verify":
		a.runCommand("pacman -Qk", false, "pacman", "-Qk")
	case "db-check":
		a.runCommand("pacman -Dk", false, "pacman", "-Dk")
	case "mirrors":
		a.confirmRun("Reflector mirror refresh", true, "reflector", "--latest", "20", "--sort", "rate", "--save", "/etc/pacman.d/mirrorlist")
	case "keyring":
		a.confirmRun("Refresh pacman keyring", true, "pacman-key", "--refresh-keys")
	case "paccache":
		a.confirmRun("Trim package cache", true, "paccache", "-r", "-k", "2")
	case "daemon-reload":
		a.confirmRun("systemctl daemon-reload", true, "systemctl", "daemon-reload")
	case "user-daemon-reload":
		a.runCommand("systemctl --user daemon-reload", false, "systemctl", "--user", "daemon-reload")
	case "mkinitcpio":
		a.confirmRun("Regenerate initramfs images", true, "mkinitcpio", "-P")
	case "grub":
		a.confirmRun("Regenerate GRUB configuration", true, "grub-mkconfig", "-o", "/boot/grub/grub.cfg")
	case "backup-db":
		a.confirmRun("Backup pacman local database", true, "tar", "-cJf", "pacman-local-db.tar.xz", "-C", "/var/lib/pacman", "local")
	case "export-all":
		a.exportCommand("pacman -Q", false, "pacman", "-Q")
	case "export-native":
		a.exportCommand("pacman -Qne", false, "pacman", "-Qne")
	case "checkupdates":
		a.runCommand("checkupdates", false, "checkupdates")
	case "foreign":
		a.runCommand("pacman -Qm", false, "pacman", "-Qm")
	case "native-explicit":
		a.runCommand("pacman -Qne", false, "pacman", "-Qne")
	case "failed-services":
		a.runCommand("systemctl --failed", false, "systemctl", "--failed", "--no-pager", "--no-legend")
	case "journal-errors":
		a.runCommand("journalctl errors", false, "journalctl", "-p", "err", "-n", "120", "--no-pager")
	case "pacnew":
		a.runCommand(".pacnew scan", false, "bash", "-lc", "find /etc -name '*.pacnew' 2>/dev/null | sort")
	case "ip":
		a.runCommand("ip -br a", false, "ip", "-br", "a")
	case "uname":
		a.runCommand("uname -a", false, "uname", "-a")
	case "df":
		a.runCommand("df -h", false, "df", "-h", "/", "/var", "/home")
	case "memory":
		a.runCommand("free -h", false, "free", "-h")
	case "pacdiff":
		a.runCommand("pacdiff --safe", false, "pacdiff", "--safe")
	case "pkgfile":
		a.confirmRun("Update pkgfile database", true, "pkgfile", "-u")
	case "flatpak-version":
		a.runCommand("flatpak --version", false, "flatpak", "--version")
	case "flatpak-repair":
		a.confirmRun("Flatpak repair", false, "flatpak", "repair", "--noninteractive")
	case "flatpak-update":
		a.confirmRun("Flatpak update", false, "flatpak", "update", "-y")
	case "flatpak-unused":
		a.confirmRun("Remove unused Flatpak data", false, "flatpak", "uninstall", "--unused", "-y")
	case "fwupdmgr-devices":
		a.runCommand("fwupdmgr devices", false, "fwupdmgr", "get-devices", "--no-pager")
	case "fwupdmgr-updates":
		a.runCommand("fwupdmgr updates", false, "fwupdmgr", "get-updates", "--no-pager")
	case "file-search":
		if len(args) > 0 {
			a.runCommand("pacman -Fs", false, "pacman", "-Fs", strings.Join(args, " "))
		}
	case "file-owner":
		if len(args) > 0 {
			a.runCommand("pacman -Qo", false, "pacman", "-Qo", strings.Join(args, " "))
		}
	default:
		fmt.Println("Unknown command. Type help.")
	}
}
func (a *App) runSystemAction(action string) {
	mapping := map[string]string{"update": "upgrade", "clean": "clean", "sync": "sync", "fixdeps": "db-check", "paccache": "paccache"}
	if key, ok := mapping[action]; ok {
		a.runTool(key, nil)
		return
	}
	a.appendLog("Unknown system action: " + action)
}

func (a *App) exportCommand(label string, priv bool, args ...string) {
	out, errOut, code := runCapture(180*time.Second, args[0], args[1:]...)
	if errOut != "" {
		out += errOut
	}
	path := "all-packages.txt"
	if strings.Contains(strings.ToLower(label), "qne") {
		path = "native-explicit-packages.txt"
	}
	if code != 0 {
		a.appendLog(label + " failed: " + strings.TrimSpace(out))
		return
	}
	if err := os.WriteFile(path, []byte(out), 0644); err != nil {
		a.appendLog("Export failed: " + err.Error())
		return
	}
	a.appendLog("Exported package list to " + path)
}

func (a *App) confirmRun(label string, priv bool, args ...string) {
	if a.confirm(label + "? ") {
		a.runCommand(label, priv, args...)
	}
}
func (a *App) runCommand(label string, priv bool, args ...string) {
	if len(args) == 0 {
		return
	}
	a.appendHistory(label, strings.Join(args, " "))
	a.appendLog("$ " + strings.Join(args, " "))
	cmdArgs := args
	if priv && os.Geteuid() != 0 {
		if p := privTool(); p != "" {
			cmdArgs = append([]string{p}, args...)
		} else {
			a.appendLog("No authentication tool found (sudo/doas).")
			return
		}
	}
	cmd := exec.Command(cmdArgs[0], cmdArgs[1:]...)
	cmd.Stdout = io.MultiWriter(os.Stdout, &logWriter{a: a})
	cmd.Stderr = io.MultiWriter(os.Stderr, &logWriter{a: a})
	cmd.Stdin = os.Stdin
	fmt.Printf("\n%sRunning %s…%s\n", yellow, label, reset)
	err := cmd.Run()
	if err != nil {
		a.appendLog(fmt.Sprintf("%s failed: %v", label, err))
	} else {
		a.appendLog(label + " completed successfully")
	}
}

type logWriter struct{ a *App }

func (w *logWriter) Write(p []byte) (int, error) {
	for _, line := range strings.Split(strings.TrimRight(string(p), "\n"), "\n") {
		w.a.appendLog(line)
	}
	return len(p), nil
}
func privTool() string {
	if commandExists("sudo") {
		return "sudo"
	}
	if commandExists("doas") {
		return "doas"
	}
	return ""
}
func aurHelper() string {
	if commandExists("yay") {
		return "yay"
	}
	if commandExists("paru") {
		return "paru"
	}
	return ""
}

func (a *App) setSetting(args []string) {
	if len(args) < 2 {
		fmt.Println("Usage: set <key> <value>")
		return
	}
	key, val := strings.ToLower(args[0]), strings.ToLower(args[1])
	b := val == "true" || val == "1" || val == "yes"
	switch key {
	case "auto_check_updates":
		a.settings.AutoCheckUpdates = b
	case "show_security_alerts":
		a.settings.ShowSecurity = b
	case "confirm_install":
		a.settings.ConfirmInstall = b
	case "confirm_remove":
		a.settings.ConfirmRemove = b
	case "search_official":
		a.settings.SearchOfficial = b
	case "search_aur":
		a.settings.SearchAUR = b
	case "search_flatpak":
		a.settings.SearchFlatpak = b
	case "search_appimage":
		a.settings.SearchAppImage = b
	case "max_search_results":
		if n, e := strconv.Atoi(args[1]); e == nil {
			a.settings.MaxResults = n
		}
	default:
		fmt.Println("Unknown setting.")
	}
}
func (a *App) saveSettings() {
	_ = os.MkdirAll(configDir, 0755)
	if writeJSON(configFile, a.settings) == nil {
		a.appendLog("Settings saved")
	}
}
func (a *App) appendHistory(label, cmd string) {
	a.history = append(a.history, HistoryEntry{Time: time.Now().Format("2006-01-02 15:04:05"), Label: label, Cmd: cmd})
	if len(a.history) > 200 {
		a.history = a.history[len(a.history)-200:]
	}
	_ = os.MkdirAll(configDir, 0755)
	_ = writeJSON(historyFile, a.history)
}
func (a *App) appendLog(s string) {
	stamp := time.Now().Format("15:04:05")
	a.logLines = append(a.logLines, fmt.Sprintf("[%s] %s", stamp, s))
	if len(a.logLines) > 1000 {
		a.logLines = a.logLines[len(a.logLines)-1000:]
	}
	if debugEnabled {
		fmt.Fprintln(os.Stderr, "[DEBUG-NUITKA]", s)
	}
}
func (a *App) toggleFavorite(args []string) {
	if len(args) == 0 {
		return
	}
	n, _ := strconv.Atoi(args[0])
	if n < 1 || n > len(a.lastResults) {
		return
	}
	name := a.lastResults[n-1].Name
	a.favorites[name] = !a.favorites[name]
	var list []string
	for k, v := range a.favorites {
		if v {
			list = append(list, k)
		}
	}
	sort.Strings(list)
	_ = writeJSON(favoritesFile, list)
}
func (a *App) exportVisible(args []string) {
	path := "pkger-visible-packages.txt"
	if len(args) > 0 {
		path = strings.Join(args, " ")
	}
	var b strings.Builder
	for _, p := range a.lastResults {
		b.WriteString(fmt.Sprintf("%s/%s  %s\n", p.Repository, p.Name, p.Version))
	}
	if os.WriteFile(path, []byte(b.String()), 0644) == nil {
		a.appendLog("Exported visible packages to " + path)
	}
}
func (a *App) exportLog(args []string) {
	path := "pkger-log-" + time.Now().Format("20060102-150405") + ".txt"
	if len(args) > 0 {
		path = strings.Join(args, " ")
	}
	if os.WriteFile(path, []byte(strings.Join(a.logLines, "\n")+"\n"), 0644) == nil {
		a.appendLog("Log exported to " + path)
	}
}
func (a *App) showHistory() {
	for i := len(a.history) - 1; i >= 0 && i >= len(a.history)-50; i-- {
		h := a.history[i]
		fmt.Printf("[%s] %s\n  %s\n", h.Time, h.Label, h.Cmd)
	}
}
func (a *App) loadPacmanLog() {
	b, err := os.ReadFile("/var/log/pacman.log")
	if err != nil {
		a.appendLog("Cannot read /var/log/pacman.log: " + err.Error())
		return
	}
	lines := strings.Split(string(b), "\n")
	if len(lines) > 500 {
		lines = lines[len(lines)-500:]
	}
	a.logLines = lines
}
func (a *App) confirm(prompt string) bool {
	fmt.Printf("%s%s [y/N] %s", yellow, prompt, reset)
	line, _ := a.reader.ReadString('\n')
	v := strings.ToLower(strings.TrimSpace(line))
	return v == "y" || v == "yes"
}

func systemStats() SystemStats {
	st := SystemStats{Kernel: "-", Uptime: "-", Load: "-", DiskFree: "-", MemUsedPct: "-"}
	if o, _, c := runCapture(5*time.Second, "uname", "-r"); c == 0 {
		st.Kernel = strings.TrimSpace(o)
	}
	if b, e := os.ReadFile("/proc/uptime"); e == nil {
		f, _ := strconv.ParseFloat(strings.Fields(string(b))[0], 64)
		mins := int(f) / 60
		days := mins / 1440
		mins %= 1440
		hrs := mins / 60
		mins %= 60
		if days > 0 {
			st.Uptime = fmt.Sprintf("%dd %dh %dm", days, hrs, mins)
		} else {
			st.Uptime = fmt.Sprintf("%dh %dm", hrs, mins)
		}
	}
	if b, e := os.ReadFile("/proc/loadavg"); e == nil {
		st.Load = strings.Join(strings.Fields(string(b))[:min(3, len(strings.Fields(string(b))))], " ")
	}
	if o, _, c := runCapture(10*time.Second, "df", "-h", "/"); c == 0 {
		ls := strings.Split(strings.TrimSpace(o), "\n")
		if len(ls) > 1 {
			p := strings.Fields(ls[1])
			if len(p) > 3 {
				st.DiskFree = p[3]
			}
		}
	}
	if b, e := os.ReadFile("/proc/meminfo"); e == nil {
		var total, avail int64
		for _, line := range strings.Split(string(b), "\n") {
			p := strings.Fields(line)
			if len(p) >= 2 && p[0] == "MemTotal:" {
				total, _ = strconv.ParseInt(p[1], 10, 64)
			}
			if len(p) >= 2 && p[0] == "MemAvailable:" {
				avail, _ = strconv.ParseInt(p[1], 10, 64)
			}
		}
		if total > 0 {
			st.MemUsedPct = fmt.Sprintf("%d%%", 100-avail*100/total)
		}
	}
	st.CacheSize = dirSize("/var/cache/pacman/pkg")
	return st
}
func dirSize(root string) int64 {
	var total int64
	filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err == nil && info != nil && !info.IsDir() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func fetchNews(limit int) []NewsItem {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", NewsRSS, nil)
	req.Header.Set("User-Agent", AppName+"/"+AppVersion)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	xml := string(b)
	var out []NewsItem
	for _, block := range strings.Split(xml, "<item>")[1:] {
		if len(out) >= limit {
			break
		}
		get := func(tag string) string {
			start := "<" + tag + ">"
			end := "</" + tag + ">"
			i := strings.Index(block, start)
			if i < 0 {
				return ""
			}
			i += len(start)
			j := strings.Index(block[i:], end)
			if j < 0 {
				return ""
			}
			return decodeXML(block[i : i+j])
		}
		out = append(out, NewsItem{Title: get("title"), Link: get("link"), Summary: get("description"), Date: get("pubDate")})
	}
	return out
}
func decodeXML(s string) string {
	return strings.NewReplacer("&amp;", "&", "&lt;", "<", "&gt;", ">", "&quot;", "\"", "&#39;", "'").Replace(strings.TrimSpace(s))
}
func stripHTML(s string) string {
	var b strings.Builder
	inside := false
	for _, r := range s {
		if r == '<' {
			inside = true
			continue
		}
		if r == '>' {
			inside = false
			continue
		}
		if !inside {
			b.WriteRune(r)
		}
	}
	return strings.TrimSpace(b.String())
}

func classifyRepo(r string) string {
	l := strings.ToLower(r)
	if l == "core" || l == "extra" || l == "multilib" || l == "community" {
		return "official"
	}
	if strings.Contains(l, "testing") {
		return "testing"
	}
	if strings.Contains(l, "chaotic") || strings.Contains(l, "blackarch") || strings.Contains(l, "archlinuxcn") {
		return "third-party"
	}
	return "community"
}
func humanSize(n int64) string {
	units := []string{"B", "KB", "MB", "GB", "TB"}
	f := float64(n)
	for _, u := range units {
		if f < 1024 {
			return fmt.Sprintf("%.1f %s", f, u)
		}
		f /= 1024
	}
	return fmt.Sprintf("%.1f PB", f)
}
func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:max(0, n-1)]) + "…"
}
func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
func readJSON(path string, v any) error {
	b, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	return json.Unmarshal(b, v)
}
func writeJSON(path string, v any) error {
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	return os.WriteFile(path, b, 0644)
}

// Keep a reference to os/user so builds on minimal systems verify the same user model
// used by the original application when selecting a home directory.
var _ = user.Current

// ---------------- GTK4 GUI ----------------

type GUIApp struct {
	core                                                                        *App
	application                                                                 *gtk.Application
	window                                                                      *gtk.ApplicationWindow
	stack                                                                       *gtk.Stack
	title                                                                       *gtk.Label
	status                                                                      *gtk.Label
	progress                                                                    *gtk.ProgressBar
	homeHealth, homeInstalled, homeUpdates, homeSecurity, homeCache, homeKernel *gtk.Label
	homeUptime, homeLoad, homeDisk, homeRAM, homeRepos, homeRefresh             *gtk.Label
	searchEntry                                                                 *gtk.Entry
	sourceDrop                                                                  *gtk.DropDown
	sortDrop                                                                    *gtk.DropDown
	filterEntry                                                                 *gtk.Entry
	installedOnly                                                               *gtk.CheckButton
	installResultLabel                                                          *gtk.Label
	packageChecks                                                               map[string]*gtk.CheckButton
	selectedPackages                                                            map[string]bool
	localPackageEntry                                                           *gtk.Entry
	localFileDialog                                                             *gtk.FileChooserNative
	localFilesList                                                              *gtk.ListBox
	localFilesCount                                                             *gtk.Label
	localPackagePaths                                                           []string
	packageList                                                                 *gtk.ListBox
	packageDetails                                                              *gtk.TextView
	packageOutput                                                               *gtk.TextView
	visiblePkgs                                                                 []Package
	selectedIndex                                                               int
	updateList                                                                  *gtk.ListBox
	updateChecks                                                                map[int]*gtk.CheckButton
	repoList, repoPackages                                                      *gtk.ListBox
	repoFilter, repoPackageFilter                                               *gtk.Entry
	repoInstalledOnly                                                           *gtk.CheckButton
	repoSortDrop                                                                *gtk.DropDown
	repoStats                                                                   *gtk.Label
	repoDetails, repoOutput                                                     *gtk.TextView
	repoPackageChecks                                                           map[string]*gtk.CheckButton
	repoSelected                                                                map[string]bool
	repoVisiblePkgs                                                             []RepoPackage
	selectedRepo                                                                string
	imageList                                                                   *gtk.ListBox
	imageDetails                                                                *gtk.TextView
	selectedImage                                                               int
	logView                                                                     *gtk.TextView
	newsView                                                                    *gtk.TextView
	settingsChecks                                                              map[string]*gtk.CheckButton
	authDialog                                                                  *gtk.Dialog
	opMu                                                                        sync.Mutex
	opRunning                                                                   bool
}

var installSources = []string{"Unified", "Official", "AUR", "Installed", "Flatpak", "AppImage", "Developer"}
var installSourceKeys = []string{"unified", "official", "aur", "installed", "flatpak", "appimage", "developer"}
var packageSorts = []string{"Name", "Repository", "Version", "Installed"}

func guiMain() {
	core := NewApp()
	application := gtk.NewApplication("com.almezali.pkger.gtk4", gio.ApplicationFlagsNone)
	application.ConnectActivate(func() {
		gui := newGUIApp(core, application)
		gui.build()
		gui.window.Present()
	})
	application.Run(os.Args)
}

func newGUIApp(core *App, application *gtk.Application) *GUIApp {
	return &GUIApp{core: core, application: application, selectedIndex: -1, selectedImage: -1, updateChecks: map[int]*gtk.CheckButton{}, settingsChecks: map[string]*gtk.CheckButton{}}
}

func (g *GUIApp) build() {
	if g.selectedPackages == nil {
		g.selectedPackages = map[string]bool{}
	}
	if g.repoSelected == nil {
		g.repoSelected = map[string]bool{}
	}
	g.window = gtk.NewApplicationWindow(g.application)
	g.window.SetTitle("PKGER v" + AppVersion + " — Arch Package Manager")
	g.window.SetDefaultSize(1240, 820)
	g.applyCSS()

	root := gtk.NewBox(gtk.OrientationHorizontal, 0)
	root.AddCSSClass("window-root")
	g.window.SetChild(root)
	root.Append(g.buildSidebar())

	main := gtk.NewBox(gtk.OrientationVertical, 0)
	main.SetHExpand(true)
	main.SetVExpand(true)
	root.Append(main)

	header := gtk.NewBox(gtk.OrientationHorizontal, 10)
	header.AddCSSClass("topbar")
	header.SetMarginStart(20)
	header.SetMarginEnd(20)
	header.SetMarginTop(14)
	header.SetMarginBottom(10)
	g.title = gtk.NewLabel("Home")
	g.title.AddCSSClass("page-title")
	g.title.SetHExpand(true)
	g.title.SetXAlign(0)
	header.Append(g.title)
	quickSearch := gtk.NewButtonWithLabel("Search")
	quickSearch.AddCSSClass("flat-button")
	quickSearch.ConnectClicked(func() { g.switchPage("install"); g.searchEntry.GrabFocus() })
	header.Append(quickSearch)
	refresh := gtk.NewButtonWithLabel("Refresh")
	refresh.AddCSSClass("flat-button")
	refresh.ConnectClicked(func() { g.refreshCurrent() })
	header.Append(refresh)
	main.Append(header)

	g.stack = gtk.NewStack()
	g.stack.SetTransitionType(gtk.StackTransitionTypeSlideLeftRight)
	g.stack.SetVExpand(true)
	g.stack.SetHExpand(true)
	g.stack.AddTitled(g.homePage(), "home", "Home")
	g.stack.AddTitled(g.installPage(), "install", "Install")
	g.stack.AddTitled(g.updatesPage(), "updates", "Updates")
	g.stack.AddTitled(g.reposPage(), "repos", "Sources")
	g.stack.AddTitled(g.imagesPage(), "images", "AppImages")
	g.stack.AddTitled(g.toolsPage(), "tools", "Tools")
	g.stack.AddTitled(g.settingsPage(), "settings", "Settings")
	g.stack.AddTitled(g.logsPage(), "logs", "Logs")
	g.stack.AddTitled(g.newsPage(), "news", "News")
	main.Append(g.stack)

	bottom := gtk.NewBox(gtk.OrientationHorizontal, 10)
	bottom.AddCSSClass("statusbar")
	bottom.SetMarginStart(16)
	bottom.SetMarginEnd(16)
	bottom.SetMarginTop(8)
	bottom.SetMarginBottom(10)
	g.status = gtk.NewLabel("Ready")
	g.status.SetXAlign(0)
	g.status.SetHExpand(true)
	bottom.Append(g.status)
	g.progress = gtk.NewProgressBar()
	g.progress.SetHExpand(true)
	g.progress.SetVisible(false)
	bottom.Append(g.progress)
	main.Append(bottom)
	g.switchPage("home")
	go g.loadStartup()
}

func (g *GUIApp) applyCSS() {
	css := `
	.window-root { background: @window_bg_color; }
	.sidebar { background: mix(@window_bg_color, @theme_fg_color, .045); border-right: 1px solid @borders; padding: 14px 9px; min-width: 205px; }
	.brand { padding: 10px 12px 22px; }
	.brand-name { font-size: 21px; font-weight: 900; letter-spacing: 1.2px; }
	.brand-version { color: alpha(@theme_fg_color,.52); font-size: 11px; }
	.nav-section { color: alpha(@theme_fg_color,.43); font-size: 10px; font-weight: 800; letter-spacing: 1px; margin: 13px 10px 5px; }
	.nav-button { border-radius: 9px; margin: 2px 0; padding: 8px 11px; background: transparent; text-align: left; }
	.nav-button:hover { background: alpha(@theme_fg_color,.08); }
	.nav-button.active { background: alpha(@accent_color,.18); color: @accent_color; font-weight: 800; }
	.topbar { border-bottom: 1px solid @borders; }
	.page-title { font-size: 19px; font-weight: 850; }
	.section-title { font-size: 21px; font-weight: 850; }
	.subtitle { color: alpha(@theme_fg_color,.58); font-size: 12px; }
	.card { background: mix(@theme_bg_color,@theme_fg_color,.05); border: 1px solid @borders; border-radius: 14px; padding: 15px; }
	.stat-title { color: alpha(@theme_fg_color,.52); font-size: 10px; font-weight: 850; letter-spacing: .8px; }
	.stat-value { font-size: 22px; font-weight: 850; margin-top: 3px; }
	.metric-line { color: alpha(@theme_fg_color,.68); font-size: 12px; }
	.search-box { padding: 13px; background: alpha(@theme_fg_color,.045); border: 1px solid @borders; border-radius: 12px; }
	.result-row { padding: 11px 13px; border-bottom: 1px solid alpha(@borders,.65); }
	.result-name { font-weight: 750; }
	.result-meta { color: alpha(@theme_fg_color,.57); font-size: 11px; }
	.details { background: @view_bg_color; border: 1px solid @borders; border-radius: 10px; padding: 10px; font-family: monospace; }
	.source-card { background: alpha(@theme_fg_color,.035); border: 1px solid @borders; border-radius: 12px; padding: 10px; }
	.source-row { padding: 9px 10px; border-radius: 8px; margin: 2px 0; }
	.source-row:hover { background: alpha(@theme_fg_color,.08); }
	.local-files { background: alpha(@theme_fg_color,.035); border: 1px solid @borders; border-radius: 10px; padding: 7px; }
	.statusbar { border-top: 1px solid @borders; color: alpha(@theme_fg_color,.72); font-size: 12px; }
	.suggested-action { font-weight: 750; }
	.destructive-action { color: @error_color; }
	.card-title { font-size: 14px; font-weight: 800; }
	.badge { border-radius: 8px; padding: 3px 8px; background: alpha(@accent_color,.15); color: @accent_color; font-size: 11px; font-weight: 700; }
	.auth-card { background: alpha(@theme_fg_color,.035); border: 1px solid @borders; border-radius: 14px; padding: 14px; }
	.auth-card entry { border-radius: 9px; padding: 10px; }
	`
	provider := gtk.NewCSSProvider()
	provider.LoadFromString(css)
	gtk.StyleContextAddProviderForDisplay(gdk.DisplayGetDefault(), provider, gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
}

func (g *GUIApp) buildSidebar() *gtk.Box {
	side := gtk.NewBox(gtk.OrientationVertical, 0)
	side.AddCSSClass("sidebar")
	brand := gtk.NewBox(gtk.OrientationVertical, 2)
	brand.AddCSSClass("brand")
	name := gtk.NewLabel("PKGER")
	name.AddCSSClass("brand-name")
	name.SetXAlign(0)
	ver := gtk.NewLabel("v" + AppVersion + " · Go + GTK4")
	ver.AddCSSClass("brand-version")
	ver.SetXAlign(0)
	brand.Append(name)
	brand.Append(ver)
	side.Append(brand)
	sections := []struct {
		title string
		items []struct{ id, label string }
	}{{"MAIN", []struct{ id, label string }{{"home", "Home"}}}, {"PACKAGES", []struct{ id, label string }{{"install", "Install"}, {"updates", "Updates"}}}, {"SYSTEM", []struct{ id, label string }{{"repos", "Sources"}, {"images", "AppImages"}, {"tools", "Tools"}, {"settings", "Settings"}}}, {"MORE", []struct{ id, label string }{{"logs", "Logs"}, {"news", "News"}}}}
	for _, section := range sections {
		l := gtk.NewLabel(section.title)
		l.AddCSSClass("nav-section")
		l.SetXAlign(0)
		side.Append(l)
		for _, item := range section.items {
			b := gtk.NewButtonWithLabel(item.label)
			b.AddCSSClass("nav-button")
			id := item.id
			b.ConnectClicked(func() { g.switchPage(id) })
			side.Append(b)
		}
	}
	spacer := gtk.NewBox(gtk.OrientationVertical, 0)
	spacer.SetVExpand(true)
	side.Append(spacer)
	foot := gtk.NewLabel("Arch Linux package manager\npacman · AUR · Flatpak · AppImage")
	foot.AddCSSClass("brand-version")
	foot.SetXAlign(0)
	foot.SetMarginTop(10)
	foot.SetMarginBottom(8)
	foot.SetMarginStart(10)
	side.Append(foot)
	return side
}

func (g *GUIApp) homePage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 15)
	outer.SetMarginTop(22)
	outer.SetMarginBottom(22)
	outer.SetMarginStart(24)
	outer.SetMarginEnd(24)
	welcome := gtk.NewLabel("Welcome to PKGER")
	welcome.AddCSSClass("section-title")
	welcome.SetXAlign(0)
	outer.Append(welcome)
	sub := gtk.NewLabel(detectDistro() + " — practical package management with real-time system insight")
	sub.AddCSSClass("subtitle")
	sub.SetXAlign(0)
	outer.Append(sub)
	grid := gtk.NewGrid()
	grid.SetColumnSpacing(10)
	grid.SetRowSpacing(10)
	grid.SetHExpand(true)
	g.homeHealth = g.addMetric(grid, "SYSTEM HEALTH", "—", 0, 0)
	g.homeInstalled = g.addMetric(grid, "INSTALLED PACKAGES", "—", 1, 0)
	g.homeUpdates = g.addMetric(grid, "PENDING UPDATES", "—", 2, 0)
	g.homeSecurity = g.addMetric(grid, "SECURITY UPDATES", "—", 3, 0)
	g.homeCache = g.addMetric(grid, "PACMAN CACHE", "—", 0, 1)
	g.homeKernel = g.addMetric(grid, "KERNEL", "—", 1, 1)
	g.homeRepos = g.addMetric(grid, "REPOSITORIES", "—", 2, 1)
	g.homeRefresh = g.addMetric(grid, "LAST REFRESH", "—", 3, 1)
	outer.Append(grid)
	info := gtk.NewBox(gtk.OrientationVertical, 8)
	info.AddCSSClass("card")
	headline := gtk.NewLabel("Live system overview")
	headline.AddCSSClass("card-title")
	headline.SetXAlign(0)
	info.Append(headline)
	g.homeUptime = gtk.NewLabel("Uptime: —")
	g.homeUptime.AddCSSClass("metric-line")
	g.homeUptime.SetXAlign(0)
	info.Append(g.homeUptime)
	g.homeLoad = gtk.NewLabel("Load average: —")
	g.homeLoad.AddCSSClass("metric-line")
	g.homeLoad.SetXAlign(0)
	info.Append(g.homeLoad)
	g.homeDisk = gtk.NewLabel("Disk free: —")
	g.homeDisk.AddCSSClass("metric-line")
	g.homeDisk.SetXAlign(0)
	info.Append(g.homeDisk)
	g.homeRAM = gtk.NewLabel("Memory used: —")
	g.homeRAM.AddCSSClass("metric-line")
	g.homeRAM.SetXAlign(0)
	info.Append(g.homeRAM)
	outer.Append(info)
	actions := gtk.NewBox(gtk.OrientationHorizontal, 8)
	for _, it := range []struct{ label, page string }{{"Review Updates", "updates"}, {"Search Packages", "install"}, {"Manage Sources", "repos"}, {"Open Tools", "tools"}} {
		b := gtk.NewButtonWithLabel(it.label)
		page := it.page
		b.ConnectClicked(func() { g.switchPage(page) })
		actions.Append(b)
	}
	outer.Append(actions)
	note := gtk.NewLabel("All values on this page are collected from the local system: pacman, /proc, df, and installed optional tools.")
	note.AddCSSClass("subtitle")
	note.SetWrap(true)
	note.SetXAlign(0)
	outer.Append(note)
	sw := gtk.NewScrolledWindow()
	sw.SetChild(outer)
	sw.SetVExpand(true)
	return sw
}
func (g *GUIApp) addMetric(grid *gtk.Grid, title, value string, col, row int) *gtk.Label {
	box := gtk.NewBox(gtk.OrientationVertical, 3)
	box.AddCSSClass("card")
	box.SetHExpand(true)
	t := gtk.NewLabel(title)
	t.AddCSSClass("stat-title")
	t.SetXAlign(0)
	v := gtk.NewLabel(value)
	v.AddCSSClass("stat-value")
	v.SetXAlign(0)
	box.Append(t)
	box.Append(v)
	grid.Attach(box, col, row, 1, 1)
	return v
}

func (g *GUIApp) installPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 10)
	outer.SetMarginTop(15)
	outer.SetMarginBottom(15)
	outer.SetMarginStart(16)
	outer.SetMarginEnd(16)

	header := gtk.NewBox(gtk.OrientationHorizontal, 8)
	header.AddCSSClass("search-box")
	g.searchEntry = gtk.NewEntry()
	g.searchEntry.SetPlaceholderText("Search packages by name or description…")
	g.searchEntry.SetHExpand(true)
	g.searchEntry.ConnectActivate(func() { g.doSearch() })
	header.Append(g.searchEntry)
	search := gtk.NewButtonWithLabel("Search")
	search.AddCSSClass("suggested-action")
	search.ConnectClicked(func() { g.doSearch() })
	header.Append(search)
	outer.Append(header)

	controls := gtk.NewBox(gtk.OrientationHorizontal, 8)
	controls.Append(gtk.NewLabel("Source"))
	g.sourceDrop = gtk.NewDropDown(gtk.NewStringList(installSources), nil)
	controls.Append(g.sourceDrop)
	controls.Append(gtk.NewLabel("Sort"))
	g.sortDrop = gtk.NewDropDown(gtk.NewStringList(packageSorts), nil)
	g.sortDrop.ConnectActivate(func() { g.renderPackages() })
	controls.Append(g.sortDrop)
	g.filterEntry = gtk.NewEntry()
	g.filterEntry.SetPlaceholderText("Filter visible results…")
	g.filterEntry.SetHExpand(true)
	g.filterEntry.ConnectChanged(func() { g.renderPackages() })
	controls.Append(g.filterEntry)
	g.installedOnly = gtk.NewCheckButtonWithLabel("Installed only")
	g.installedOnly.ConnectToggled(func() { g.renderPackages() })
	controls.Append(g.installedOnly)
	outer.Append(controls)

	meta := gtk.NewBox(gtk.OrientationHorizontal, 8)
	metaLabel := gtk.NewLabel("Select packages with the checkboxes. Search results stay responsive while commands run in the background.")
	metaLabel.AddCSSClass("subtitle")
	metaLabel.SetHExpand(true)
	metaLabel.SetXAlign(0)
	meta.Append(metaLabel)
	g.installResultLabel = gtk.NewLabel("0 results")
	g.installResultLabel.AddCSSClass("badge")
	meta.Append(g.installResultLabel)
	outer.Append(meta)

	body := gtk.NewBox(gtk.OrientationHorizontal, 12)
	body.SetVExpand(true)
	left := gtk.NewBox(gtk.OrientationVertical, 7)
	left.SetHExpand(true)
	left.SetVExpand(true)
	g.packageList = gtk.NewListBox()
	g.packageList.SetSelectionMode(gtk.SelectionSingle)
	g.packageList.ConnectRowSelected(func(row *gtk.ListBoxRow) {
		if row != nil {
			i := row.Index()
			if i >= 0 && i < len(g.visiblePkgs) {
				g.selectedIndex = i
				g.showPackage(g.visiblePkgs[i])
			}
		}
	})
	sw := gtk.NewScrolledWindow()
	sw.SetChild(g.packageList)
	sw.SetVExpand(true)
	left.Append(sw)

	selectRow := gtk.NewBox(gtk.OrientationHorizontal, 7)
	selectAll := gtk.NewButtonWithLabel("Select all")
	selectAll.ConnectClicked(func() { g.setPackageSelection(true) })
	clearAll := gtk.NewButtonWithLabel("Clear")
	clearAll.ConnectClicked(func() { g.setPackageSelection(false) })
	install := gtk.NewButtonWithLabel("Install selected")
	install.AddCSSClass("suggested-action")
	install.ConnectClicked(func() { g.installSelected() })
	remove := gtk.NewButtonWithLabel("Remove selected")
	remove.AddCSSClass("destructive-action")
	remove.ConnectClicked(func() { g.removeSelected() })
	selectRow.Append(selectAll)
	selectRow.Append(clearAll)
	selectRow.Append(install)
	selectRow.Append(remove)
	left.Append(selectRow)

	localRow := gtk.NewBox(gtk.OrientationHorizontal, 7)
	g.localPackageEntry = gtk.NewEntry()
	g.localPackageEntry.SetPlaceholderText("No local Arch packages selected")
	g.localPackageEntry.SetEditable(false)
	g.localPackageEntry.SetHExpand(true)
	localRow.Append(g.localPackageEntry)
	browse := gtk.NewButtonWithLabel("Browse files")
	browse.ConnectClicked(func() { g.browseLocalPackages() })
	localRow.Append(browse)
	clearLocal := gtk.NewButtonWithLabel("Clear")
	clearLocal.ConnectClicked(func() { g.clearLocalPackages() })
	localRow.Append(clearLocal)
	left.Append(localRow)
	g.localFilesCount = gtk.NewLabel("No local packages selected")
	g.localFilesCount.AddCSSClass("subtitle")
	g.localFilesCount.SetXAlign(0)
	left.Append(g.localFilesCount)
	g.localFilesList = gtk.NewListBox()
	g.localFilesList.SetSelectionMode(gtk.SelectionNone)
	localScroll := gtk.NewScrolledWindow()
	localScroll.SetChild(g.localFilesList)
	localScroll.SetSizeRequest(-1, 92)
	localScroll.AddCSSClass("local-files")
	left.Append(localScroll)
	localInstall := gtk.NewButtonWithLabel("Install selected local packages")
	localInstall.AddCSSClass("suggested-action")
	localInstall.ConnectClicked(func() { g.installLocalPackage() })
	left.Append(localInstall)
	body.Append(left)

	right := gtk.NewBox(gtk.OrientationVertical, 7)
	right.SetSizeRequest(385, -1)
	dtitle := gtk.NewLabel("Package details")
	dtitle.AddCSSClass("stat-title")
	dtitle.SetXAlign(0)
	right.Append(dtitle)
	g.packageDetails = gtk.NewTextView()
	g.packageDetails.SetEditable(false)
	g.packageDetails.SetMonospace(true)
	g.packageDetails.AddCSSClass("details")
	ds := gtk.NewScrolledWindow()
	ds.SetChild(g.packageDetails)
	ds.SetVExpand(true)
	right.Append(ds)
	ot := gtk.NewLabel("Operation output")
	ot.AddCSSClass("stat-title")
	ot.SetXAlign(0)
	right.Append(ot)
	g.packageOutput = gtk.NewTextView()
	g.packageOutput.SetEditable(false)
	g.packageOutput.SetMonospace(true)
	g.packageOutput.AddCSSClass("details")
	osw := gtk.NewScrolledWindow()
	osw.SetChild(g.packageOutput)
	osw.SetVExpand(true)
	right.Append(osw)
	body.Append(right)
	outer.Append(body)
	return outer
}

func (g *GUIApp) updatesPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 10)
	outer.SetMarginTop(16)
	outer.SetMarginBottom(16)
	outer.SetMarginStart(16)
	outer.SetMarginEnd(16)
	row := gtk.NewBox(gtk.OrientationHorizontal, 8)
	sum := gtk.NewLabel("No update scan yet")
	sum.AddCSSClass("subtitle")
	sum.SetHExpand(true)
	sum.SetXAlign(0)
	row.Append(sum)
	for _, it := range []struct {
		label string
		fn    func()
	}{{"Refresh", func() { g.loadUpdates(false) }}, {"Sync & refresh", func() { g.loadUpdates(true) }}, {"Select all", func() { g.setUpdateSelection(true) }}, {"Security", func() { g.selectSecurity() }}, {"Apply selected", func() { g.applyUpdates() }}} {
		b := gtk.NewButtonWithLabel(it.label)
		if it.label == "Apply selected" {
			b.AddCSSClass("suggested-action")
		}
		fn := it.fn
		b.ConnectClicked(fn)
		row.Append(b)
	}
	outer.Append(row)
	g.updateList = gtk.NewListBox()
	g.updateList.SetSelectionMode(gtk.SelectionNone)
	sw := gtk.NewScrolledWindow()
	sw.SetChild(g.updateList)
	sw.SetVExpand(true)
	outer.Append(sw)
	return outer
}

func (g *GUIApp) reposPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationHorizontal, 12)
	outer.SetMarginTop(16)
	outer.SetMarginBottom(16)
	outer.SetMarginStart(16)
	outer.SetMarginEnd(16)

	left := gtk.NewBox(gtk.OrientationVertical, 8)
	left.SetSizeRequest(300, -1)
	title := gtk.NewLabel("Sources")
	title.AddCSSClass("section-title")
	title.SetXAlign(0)
	left.Append(title)
	intro := gtk.NewLabel("Compact repository navigator. Choose a source, then inspect package details below it.")
	intro.AddCSSClass("subtitle")
	intro.SetWrap(true)
	intro.SetXAlign(0)
	left.Append(intro)
	bar := gtk.NewBox(gtk.OrientationHorizontal, 6)
	g.repoFilter = gtk.NewEntry()
	g.repoFilter.SetPlaceholderText("Filter sources…")
	g.repoFilter.SetHExpand(true)
	g.repoFilter.ConnectChanged(func() { g.populateRepos() })
	bar.Append(g.repoFilter)
	refresh := gtk.NewButtonWithLabel("Refresh")
	refresh.ConnectClicked(func() { g.refreshReposAsync() })
	bar.Append(refresh)
	left.Append(bar)

	repoCard := gtk.NewBox(gtk.OrientationVertical, 4)
	repoCard.AddCSSClass("source-card")
	g.repoList = gtk.NewListBox()
	g.repoList.SetSelectionMode(gtk.SelectionSingle)
	g.repoList.ConnectRowSelected(func(row *gtk.ListBoxRow) {
		if row != nil {
			keys := g.filteredRepoNames()
			i := row.Index()
			if i >= 0 && i < len(keys) {
				g.selectedRepo = keys[i]
				g.populateRepoPackages()
			}
		}
	})
	rs := gtk.NewScrolledWindow()
	rs.SetChild(g.repoList)
	rs.SetVExpand(true)
	repoCard.Append(rs)
	left.Append(repoCard)

	detailTitle := gtk.NewLabel("Package details")
	detailTitle.AddCSSClass("stat-title")
	detailTitle.SetXAlign(0)
	left.Append(detailTitle)
	g.repoDetails = gtk.NewTextView()
	g.repoDetails.SetEditable(false)
	g.repoDetails.SetMonospace(true)
	g.repoDetails.AddCSSClass("details")
	ds := gtk.NewScrolledWindow()
	ds.SetChild(g.repoDetails)
	ds.SetSizeRequest(-1, 190)
	left.Append(ds)
	outer.Append(left)

	right := gtk.NewBox(gtk.OrientationVertical, 8)
	right.SetHExpand(true)
	right.SetVExpand(true)
	head := gtk.NewBox(gtk.OrientationHorizontal, 8)
	rt := gtk.NewLabel("Repository packages")
	rt.AddCSSClass("section-title")
	rt.SetHExpand(true)
	rt.SetXAlign(0)
	head.Append(rt)
	g.repoStats = gtk.NewLabel("Select a source")
	g.repoStats.AddCSSClass("badge")
	head.Append(g.repoStats)
	right.Append(head)
	controls := gtk.NewBox(gtk.OrientationHorizontal, 7)
	g.repoPackageFilter = gtk.NewEntry()
	g.repoPackageFilter.SetPlaceholderText("Filter package names…")
	g.repoPackageFilter.SetHExpand(true)
	g.repoPackageFilter.ConnectChanged(func() { g.populateRepoPackages() })
	controls.Append(g.repoPackageFilter)
	g.repoSortDrop = gtk.NewDropDown(gtk.NewStringList(packageSorts), nil)
	g.repoSortDrop.ConnectActivate(func() { g.populateRepoPackages() })
	controls.Append(g.repoSortDrop)
	g.repoInstalledOnly = gtk.NewCheckButtonWithLabel("Installed only")
	g.repoInstalledOnly.ConnectToggled(func() { g.populateRepoPackages() })
	controls.Append(g.repoInstalledOnly)
	right.Append(controls)

	g.repoPackages = gtk.NewListBox()
	g.repoPackages.SetSelectionMode(gtk.SelectionSingle)
	g.repoPackages.ConnectRowSelected(func(row *gtk.ListBoxRow) {
		if row != nil {
			i := row.Index()
			if i >= 0 && i < len(g.repoVisiblePkgs) {
				g.showRepoPackage(g.repoVisiblePkgs[i])
			}
		}
	})
	ps := gtk.NewScrolledWindow()
	ps.SetChild(g.repoPackages)
	ps.SetVExpand(true)
	right.Append(ps)
	actions := gtk.NewBox(gtk.OrientationHorizontal, 7)
	all := gtk.NewButtonWithLabel("Select all")
	all.ConnectClicked(func() { g.setRepoSelection(true) })
	clear := gtk.NewButtonWithLabel("Clear")
	clear.ConnectClicked(func() { g.setRepoSelection(false) })
	install := gtk.NewButtonWithLabel("Install selected")
	install.AddCSSClass("suggested-action")
	install.ConnectClicked(func() { g.installRepoSelected() })
	remove := gtk.NewButtonWithLabel("Remove selected")
	remove.AddCSSClass("destructive-action")
	remove.ConnectClicked(func() { g.removeRepoSelected() })
	export := gtk.NewButtonWithLabel("Export")
	export.ConnectClicked(func() { g.core.exportRepo(); g.setStatus("Repository list exported") })
	actions.Append(all)
	actions.Append(clear)
	actions.Append(install)
	actions.Append(remove)
	actions.Append(export)
	right.Append(actions)
	outTitle := gtk.NewLabel("Operation output")
	outTitle.AddCSSClass("stat-title")
	outTitle.SetXAlign(0)
	right.Append(outTitle)
	g.repoOutput = gtk.NewTextView()
	g.repoOutput.SetEditable(false)
	g.repoOutput.SetMonospace(true)
	g.repoOutput.AddCSSClass("details")
	osw := gtk.NewScrolledWindow()
	osw.SetChild(g.repoOutput)
	osw.SetSizeRequest(-1, 125)
	right.Append(osw)
	outer.Append(right)
	return outer
}

func (g *GUIApp) imagesPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 9)
	outer.SetMarginTop(16)
	outer.SetMarginBottom(16)
	outer.SetMarginStart(16)
	outer.SetMarginEnd(16)
	row := gtk.NewBox(gtk.OrientationHorizontal, 8)
	scan := gtk.NewButtonWithLabel("Scan locations")
	scan.ConnectClicked(func() { g.scanImages() })
	importB := gtk.NewButtonWithLabel("Import from path")
	importB.ConnectClicked(func() { g.setStatus("Copy an AppImage to ~/Applications, then choose Scan locations.") })
	launch := gtk.NewButtonWithLabel("Launch selected")
	launch.ConnectClicked(func() { g.launchImage() })
	remove := gtk.NewButtonWithLabel("Remove selected")
	remove.AddCSSClass("destructive-action")
	remove.ConnectClicked(func() { g.removeImage() })
	row.Append(scan)
	row.Append(importB)
	row.Append(launch)
	row.Append(remove)
	outer.Append(row)
	hint := gtk.NewLabel("PKGER scans ~/Applications, ~/.local/bin, and /opt for executable AppImage files.")
	hint.AddCSSClass("subtitle")
	hint.SetXAlign(0)
	outer.Append(hint)
	g.imageList = gtk.NewListBox()
	g.imageList.ConnectRowSelected(func(row *gtk.ListBoxRow) {
		if row != nil {
			g.selectedImage = row.Index()
			g.showImage()
		}
	})
	sw := gtk.NewScrolledWindow()
	sw.SetChild(g.imageList)
	sw.SetVExpand(true)
	outer.Append(sw)
	g.imageDetails = gtk.NewTextView()
	g.imageDetails.SetEditable(false)
	g.imageDetails.SetMonospace(true)
	g.imageDetails.AddCSSClass("details")
	ds := gtk.NewScrolledWindow()
	ds.SetChild(g.imageDetails)
	ds.SetVExpand(true)
	outer.Append(ds)
	return outer
}

func (g *GUIApp) toolsPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 10)
	outer.SetMarginTop(16)
	outer.SetMarginBottom(16)
	outer.SetMarginStart(16)
	outer.SetMarginEnd(16)
	title := gtk.NewLabel("System tools")
	title.AddCSSClass("section-title")
	title.SetXAlign(0)
	outer.Append(title)
	desc := gtk.NewLabel("Maintenance and diagnostics are grouped by purpose. Command output is streamed to Logs.")
	desc.AddCSSClass("subtitle")
	desc.SetXAlign(0)
	outer.Append(desc)
	groups := [][]string{{"upgrade", "sync", "clean", "deepclean", "filedb"}, {"orphans", "aur-upgrade", "verify", "db-check", "mirrors"}, {"keyring", "paccache", "daemon-reload", "user-daemon-reload", "mkinitcpio"}, {"grub", "backup-db", "export-all", "export-native", "pacman-log"}, {"checkupdates", "foreign", "native-explicit", "failed-services", "journal-errors"}, {"pacnew", "ip", "uname", "df", "memory"}, {"pacdiff", "pkgfile", "flatpak-repair", "flatpak-update", "flatpak-unused"}}
	for _, group := range groups {
		r := gtk.NewBox(gtk.OrientationHorizontal, 7)
		for _, name := range group {
			b := gtk.NewButtonWithLabel(name)
			b.SetHExpand(true)
			action := name
			b.ConnectClicked(func() { g.runTool(action) })
			r.Append(b)
		}
		outer.Append(r)
	}
	return newScrolledWindowWithChild(outer)
}

func (g *GUIApp) settingsPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 13)
	outer.SetMarginTop(20)
	outer.SetMarginBottom(20)
	outer.SetMarginStart(24)
	outer.SetMarginEnd(24)
	title := gtk.NewLabel("Settings")
	title.AddCSSClass("section-title")
	title.SetXAlign(0)
	outer.Append(title)
	desc := gtk.NewLabel("Control startup behavior, confirmations, and search coverage.")
	desc.AddCSSClass("subtitle")
	desc.SetXAlign(0)
	outer.Append(desc)
	g.settingsChecks = map[string]*gtk.CheckButton{}
	items := []struct {
		key, label string
		value      bool
	}{{"auto_check_updates", "Auto-check updates on startup", g.core.settings.AutoCheckUpdates}, {"show_security_alerts", "Show security alerts prominently", g.core.settings.ShowSecurity}, {"confirm_install", "Confirm before installing packages", g.core.settings.ConfirmInstall}, {"confirm_remove", "Confirm before removing packages", g.core.settings.ConfirmRemove}, {"search_official", "Include official pacman results", g.core.settings.SearchOfficial}, {"search_aur", "Include AUR results", g.core.settings.SearchAUR}, {"search_flatpak", "Include Flatpak results", g.core.settings.SearchFlatpak}, {"search_appimage", "Include AppImage results", g.core.settings.SearchAppImage}}
	for _, it := range items {
		c := gtk.NewCheckButtonWithLabel(it.label)
		c.SetActive(it.value)
		key := it.key
		g.settingsChecks[key] = c
		outer.Append(c)
	}
	save := gtk.NewButtonWithLabel("Save settings")
	save.AddCSSClass("suggested-action")
	save.ConnectClicked(func() { g.saveGUISettings() })
	outer.Append(save)
	return newScrolledWindowWithChild(outer)
}
func (g *GUIApp) logsPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 8)
	outer.SetMarginTop(12)
	outer.SetMarginBottom(12)
	outer.SetMarginStart(12)
	outer.SetMarginEnd(12)
	g.logView = gtk.NewTextView()
	g.logView.SetEditable(false)
	g.logView.SetMonospace(true)
	g.logView.AddCSSClass("details")
	sw := gtk.NewScrolledWindow()
	sw.SetChild(g.logView)
	sw.SetVExpand(true)
	outer.Append(sw)
	row := gtk.NewBox(gtk.OrientationHorizontal, 8)
	clear := gtk.NewButtonWithLabel("Clear")
	clear.ConnectClicked(func() { g.core.logLines = nil; g.refreshLogs() })
	export := gtk.NewButtonWithLabel("Export log")
	export.ConnectClicked(func() { g.core.exportLog(nil); g.refreshLogs() })
	row.Append(clear)
	row.Append(export)
	outer.Append(row)
	return outer
}
func (g *GUIApp) newsPage() gtk.Widgetter {
	outer := gtk.NewBox(gtk.OrientationVertical, 8)
	outer.SetMarginTop(12)
	outer.SetMarginBottom(12)
	outer.SetMarginStart(12)
	outer.SetMarginEnd(12)
	row := gtk.NewBox(gtk.OrientationHorizontal, 8)
	t := gtk.NewLabel("Arch Linux News")
	t.AddCSSClass("section-title")
	t.SetHExpand(true)
	t.SetXAlign(0)
	refresh := gtk.NewButtonWithLabel("Refresh")
	refresh.ConnectClicked(func() { g.loadNews() })
	row.Append(t)
	row.Append(refresh)
	outer.Append(row)
	g.newsView = gtk.NewTextView()
	g.newsView.SetEditable(false)
	g.newsView.SetWrapMode(gtk.WrapWord)
	sw := gtk.NewScrolledWindow()
	sw.SetChild(g.newsView)
	sw.SetVExpand(true)
	outer.Append(sw)
	return outer
}

func (g *GUIApp) switchPage(page string) {
	titles := map[string]string{"home": "Home", "install": "Install", "updates": "Updates", "repos": "Sources", "images": "AppImages", "tools": "Tools", "settings": "Settings", "logs": "Logs", "news": "News"}
	g.stack.SetVisibleChildName(page)
	g.title.SetText(titles[page])
	switch page {
	case "home":
		g.updateHome()
	case "updates":
		g.populateUpdates()
	case "repos":
		g.populateRepos()
	case "images":
		g.populateImages()
	case "logs":
		g.refreshLogs()
	case "news":
		g.loadNews()
	}
}
func (g *GUIApp) setStatus(text string) {
	if g.status == nil {
		return
	}
	glib.IdleAdd(func() { g.status.SetText(text) })
}
func (g *GUIApp) setBusy(b bool) {
	if g.progress == nil {
		return
	}
	glib.IdleAdd(func() {
		g.progress.SetVisible(b)
		if b {
			g.progress.Pulse()
		}
	})
}
func (g *GUIApp) loadStartup() {
	g.setBusy(true)
	g.setStatus("Loading live system data…")
	go func() {
		g.core.stats = systemStats()
		g.core.loadInstalled()
		g.core.loadRepos()
		g.core.scanAppImages()
		g.core.loadUpdates(false)
		g.setBusy(false)
		g.setStatus("Ready")
		glib.IdleAdd(func() { g.updateHome(); g.populateRepos(); g.populateImages(); g.populateUpdates() })
	}()
}
func (g *GUIApp) refreshCurrent() {
	switch g.stack.VisibleChildName() {
	case "updates":
		g.loadUpdates(false)
	case "repos":
		go func() { g.core.loadRepos(); glib.IdleAdd(func() { g.populateRepos() }) }()
	case "images":
		g.scanImages()
	case "news":
		g.loadNews()
	default:
		go func() { g.core.stats = systemStats(); g.core.loadInstalled(); glib.IdleAdd(func() { g.updateHome() }) }()
	}
	g.setStatus("Refreshing…")
}
func (g *GUIApp) updateHome() {
	if g.homeHealth == nil {
		return
	}
	score := healthScore(len(g.core.packages), g.core.updates, g.core.held, g.core.stats)
	sec := 0
	for _, u := range g.core.updates {
		if severity(u.Name, u.Repository, u.Source) == "Security" {
			sec++
		}
	}
	g.homeHealth.SetText(fmt.Sprintf("%d%%", score))
	g.homeInstalled.SetText(strconv.Itoa(len(g.core.packages)))
	g.homeUpdates.SetText(strconv.Itoa(len(g.core.updates)))
	g.homeSecurity.SetText(strconv.Itoa(sec))
	g.homeCache.SetText(humanSize(g.core.stats.CacheSize))
	g.homeKernel.SetText(truncate(g.core.stats.Kernel, 18))
	g.homeRepos.SetText(strconv.Itoa(len(g.core.repos)))
	g.homeRefresh.SetText(time.Now().Format("15:04:05"))
	g.homeUptime.SetText("Uptime: " + g.core.stats.Uptime)
	g.homeLoad.SetText("Load average: " + g.core.stats.Load)
	g.homeDisk.SetText("Disk free: " + g.core.stats.DiskFree)
	g.homeRAM.SetText("Memory used: " + g.core.stats.MemUsedPct)
}
func (g *GUIApp) doSearch() {
	q := strings.TrimSpace(g.searchEntry.Text())
	idx := int(g.sourceDrop.Selected())
	if idx < 0 || idx >= len(installSourceKeys) {
		idx = 0
	}
	mode := installSourceKeys[idx]
	if mode == "installed" && q == "" {
		q = "installed"
	} else if q != "" && mode != "unified" {
		q += " " + mode
	}
	if q == "" {
		g.setStatus("Enter a query or select Installed/AppImage with an empty query")
		return
	}
	g.setBusy(true)
	g.setStatus("Searching " + installSources[idx] + "…")
	go func() {
		g.core.search(q)
		g.visiblePkgs = append([]Package(nil), g.core.lastResults...)
		glib.IdleAdd(func() {
			g.renderPackages()
			g.setBusy(false)
			g.setStatus(fmt.Sprintf("Found %d packages", len(g.visiblePkgs)))
		})
	}()
}
func (g *GUIApp) renderPackages() {
	if g.packageList == nil {
		return
	}
	items := append([]Package(nil), g.visiblePkgs...)
	filter := ""
	if g.filterEntry != nil {
		filter = strings.ToLower(strings.TrimSpace(g.filterEntry.Text()))
	}
	out := items[:0]
	for _, p := range items {
		hay := strings.ToLower(p.Name + " " + p.Description + " " + p.Repository + " " + p.Version)
		if filter != "" && !strings.Contains(hay, filter) {
			continue
		}
		if g.installedOnly != nil && g.installedOnly.Active() && !p.Installed {
			continue
		}
		out = append(out, p)
	}
	items = out
	sortKey := int(g.sortDrop.Selected())
	if sortKey < 0 || sortKey >= len(packageSorts) {
		sortKey = 0
	}
	sort.SliceStable(items, func(i, j int) bool {
		switch sortKey {
		case 1:
			return strings.ToLower(items[i].Repository) < strings.ToLower(items[j].Repository)
		case 2:
			return strings.ToLower(items[i].Version) < strings.ToLower(items[j].Version)
		case 3:
			if items[i].Installed != items[j].Installed {
				return items[i].Installed
			}
			return strings.ToLower(items[i].Name) < strings.ToLower(items[j].Name)
		default:
			return strings.ToLower(items[i].Name) < strings.ToLower(items[j].Name)
		}
	})
	g.packageList.RemoveAll()
	g.visiblePkgs = items
	for _, p := range items {
		row := gtk.NewListBoxRow()
		box := gtk.NewBox(gtk.OrientationHorizontal, 10)
		box.AddCSSClass("result-row")
		name := gtk.NewLabel(p.Name)
		name.AddCSSClass("result-name")
		name.SetXAlign(0)
		name.SetHExpand(true)
		meta := gtk.NewLabel(fmt.Sprintf("%s  ·  %s  ·  %s", p.Version, p.Repository, yesNo(p.Installed)))
		meta.AddCSSClass("result-meta")
		meta.SetXAlign(1)
		box.Append(name)
		box.Append(meta)
		row.SetChild(box)
		g.packageList.Append(row)
	}
	if g.installResultLabel != nil {
		g.installResultLabel.SetText(fmt.Sprintf("%d results", len(items)))
	}
}
func (g *GUIApp) setPackageSelection(on bool) {
	if g.selectedPackages == nil {
		g.selectedPackages = map[string]bool{}
	}
	for _, p := range g.visiblePkgs {
		if on {
			g.selectedPackages[p.Name] = true
		} else {
			delete(g.selectedPackages, p.Name)
		}
		if c := g.packageChecks[p.Name]; c != nil {
			c.SetActive(on)
		}
	}
	g.renderPackages()
}

func (g *GUIApp) showPackage(p Package) {
	g.packageDetails.Buffer().SetText(fmt.Sprintf("Name: %s\nRepository: %s\nVersion: %s\nInstalled: %v\nDescription: %s\n\nLoading metadata…", p.Name, p.Repository, p.Version, p.Installed, p.Description))
	go func() {
		var out string
		if p.Repository == "flatpak" {
			out, _, _ = runCapture(35*time.Second, "flatpak", "info", p.Name)
		} else if p.Repository == "aur" {
			if h := aurHelper(); h != "" {
				out, _, _ = runCapture(35*time.Second, h, "-Si", p.Name)
			}
		} else {
			out, _, _ = runCapture(35*time.Second, "pacman", "-Si", p.Name)
			if out == "" {
				out, _, _ = runCapture(25*time.Second, "pacman", "-Qi", p.Name)
			}
		}
		if out == "" {
			out = "No additional metadata available."
		}
		text := fmt.Sprintf("Name: %s\nRepository: %s\nVersion: %s\nInstalled: %v\nDescription: %s\n\n%s", p.Name, p.Repository, p.Version, p.Installed, p.Description, out)
		glib.IdleAdd(func() { g.packageDetails.Buffer().SetText(text) })
	}()
}
func (g *GUIApp) installSelected() {
	if len(g.selectedPackages) == 0 && g.selectedIndex >= 0 && g.selectedIndex < len(g.visiblePkgs) {
		g.selectedPackages[g.visiblePkgs[g.selectedIndex].Name] = true
	}
	if len(g.selectedPackages) == 0 {
		g.setStatus("Select one or more packages first")
		return
	}
	var pac, aur, flat []string
	for _, p := range g.visiblePkgs {
		if !g.selectedPackages[p.Name] || p.Installed {
			continue
		}
		switch p.Repository {
		case "aur":
			aur = append(aur, p.Name)
		case "flatpak":
			flat = append(flat, p.Name)
		default:
			pac = append(pac, p.Name)
		}
	}
	var cmds []guiCommand
	if len(pac) > 0 {
		cmds = append(cmds, guiCommand{label: "Install official packages", privileged: true, args: append([]string{"pacman", "-S", "--noconfirm"}, pac...)})
	}
	if len(aur) > 0 {
		if h := aurHelper(); h != "" {
			cmds = append(cmds, guiCommand{label: "Install AUR packages", authRequired: true, args: append([]string{h, "-S", "--noconfirm"}, aur...)})
		} else {
			g.setStatus("Install yay or paru for AUR packages")
			return
		}
	}
	if len(flat) > 0 {
		cmds = append(cmds, guiCommand{label: "Install Flatpak packages", args: append([]string{"flatpak", "install", "-y"}, flat...)})
	}
	g.executeCommands("Install selected packages", cmds)
}

func (g *GUIApp) removeSelected() {
	if len(g.selectedPackages) == 0 && g.selectedIndex >= 0 && g.selectedIndex < len(g.visiblePkgs) {
		g.selectedPackages[g.visiblePkgs[g.selectedIndex].Name] = true
	}
	if len(g.selectedPackages) == 0 {
		g.setStatus("Select one or more packages first")
		return
	}
	var pac, flat []string
	for _, p := range g.visiblePkgs {
		if g.selectedPackages[p.Name] && p.Installed {
			if p.Repository == "flatpak" {
				flat = append(flat, p.Name)
			} else {
				pac = append(pac, p.Name)
			}
		}
	}
	var cmds []guiCommand
	if len(pac) > 0 {
		cmds = append(cmds, guiCommand{label: "Remove packages", privileged: true, args: append([]string{"pacman", "-Rns", "--noconfirm"}, pac...)})
	}
	if len(flat) > 0 {
		cmds = append(cmds, guiCommand{label: "Remove Flatpak packages", args: append([]string{"flatpak", "uninstall", "-y"}, flat...)})
	}
	g.executeCommands("Remove selected packages", cmds)
}

func (g *GUIApp) browseLocalPackages() {
	chooser := gtk.NewFileChooserNative("Select local Arch packages", &g.window.Window, gtk.FileChooserActionOpen, "Select files", "Cancel")
	chooser.SetSelectMultiple(true)
	filter := gtk.NewFileFilter()
	filter.AddPattern("*.pkg.tar.zst")
	filter.AddPattern("*.pkg.tar.xz")
	filter.AddPattern("*.pkg.tar.gz")
	chooser.SetFilter(filter)
	g.localFileDialog = chooser
	chooser.ConnectResponse(func(responseID int) {
		if responseID != int(gtk.ResponseAccept) {
			return
		}
		model := chooser.Files()
		var paths []string
		for i := uint(0); i < model.NItems(); i++ {
			obj := model.Item(i)
			if obj == nil {
				continue
			}
			file := &gio.File{Object: obj}
			if p := file.Path(); p != "" {
				paths = append(paths, p)
			}
		}
		g.localPackagePaths = paths
		g.renderLocalPackages()
		g.setStatus(fmt.Sprintf("Selected %d local packages", len(paths)))
	})
	chooser.Show()
}
func (g *GUIApp) renderLocalPackages() {
	if g.localPackageEntry == nil {
		return
	}
	g.localPackageEntry.SetText(strings.Join(g.localPackagePaths, "; "))
	if g.localFilesList != nil {
		g.localFilesList.RemoveAll()
		for _, p := range g.localPackagePaths {
			row := gtk.NewListBoxRow()
			row.SetChild(gtk.NewLabel(filepath.Base(p)))
			g.localFilesList.Append(row)
		}
	}
	if g.localFilesCount != nil {
		if len(g.localPackagePaths) == 0 {
			g.localFilesCount.SetText("No local packages selected")
		} else {
			g.localFilesCount.SetText(fmt.Sprintf("%d local packages selected", len(g.localPackagePaths)))
		}
	}
}
func (g *GUIApp) clearLocalPackages() {
	g.localPackagePaths = nil
	g.renderLocalPackages()
	g.setStatus("Local package selection cleared")
}
func (g *GUIApp) installLocalPackage() {
	if len(g.localPackagePaths) == 0 {
		g.setStatus("Browse and select one or more local Arch packages first")
		return
	}
	args := append([]string{"pacman", "-U", "--noconfirm"}, g.localPackagePaths...)
	g.executeCommands("Install local packages", []guiCommand{{label: "Install local packages", privileged: true, args: args}})
}

type guiCommand struct {
	label        string
	privileged   bool
	authRequired bool
	args         []string
}

func needsAuthorization(commands []guiCommand) bool {
	for _, command := range commands {
		if command.privileged || command.authRequired {
			return true
		}
	}
	return false
}

func (g *GUIApp) beginOperation(label string) bool {
	g.opMu.Lock()
	defer g.opMu.Unlock()
	if g.opRunning {
		g.setStatus("Another operation is already running. Please wait for it to finish.")
		return false
	}
	g.opRunning = true
	g.setStatus(label + "…")
	return true
}
func (g *GUIApp) endOperation() { g.opMu.Lock(); g.opRunning = false; g.opMu.Unlock() }

func (g *GUIApp) executeGUI(label string, privileged bool, args ...string) {
	g.executeCommands(label, []guiCommand{{label: label, privileged: privileged, args: args}})
}

func (g *GUIApp) executeCommands(label string, commands []guiCommand) {
	if len(commands) == 0 {
		g.setStatus("No eligible packages selected")
		return
	}
	if !g.beginOperation(label) {
		return
	}
	g.setBusy(true)
	if needsAuthorization(commands) && os.Geteuid() != 0 {
		tool := privTool()
		if tool == "" {
			g.endOperation()
			g.setBusy(false)
			g.setStatus("No sudo or doas found")
			return
		}
		if tool == "sudo" {
			g.showSudoDialog(label, commands)
			return
		}
		g.verifyDoasAsync(label, commands)
		return
	}
	g.runCommandsAsync(label, commands)
}

func (g *GUIApp) verifyDoasAsync(label string, commands []guiCommand) {
	go func() {
		cmd := exec.CommandContext(context.Background(), "doas", "-n", "true")
		var errOut strings.Builder
		cmd.Stderr = &errOut
		err := cmd.Run()
		detail := strings.TrimSpace(errOut.String())
		glib.IdleAdd(func() {
			if err != nil {
				g.endOperation()
				g.setBusy(false)
				if detail == "" {
					detail = "doas authorization is unavailable. Authenticate first or configure doas persist."
				}
				g.setStatus(detail)
				return
			}
			g.runCommandsAsync(label, commands)
		})
	}()
}

func (g *GUIApp) showSudoDialog(label string, commands []guiCommand) {
	if g.authDialog != nil {
		g.endOperation()
		g.setBusy(false)
		g.setStatus("Authentication dialog is already open")
		return
	}
	dialog := gtk.NewDialog()
	g.authDialog = dialog
	dialog.SetTitle("Administrator authentication")
	dialog.SetModal(true)
	dialog.SetTransientFor(&g.window.Window)
	dialog.SetDefaultSize(500, 270)
	content := dialog.ContentArea()
	content.SetMarginTop(20)
	content.SetMarginBottom(20)
	content.SetMarginStart(22)
	content.SetMarginEnd(22)
	card := gtk.NewBox(gtk.OrientationVertical, 10)
	card.AddCSSClass("auth-card")
	title := gtk.NewLabel("Administrator access required")
	title.AddCSSClass("section-title")
	title.SetXAlign(0)
	card.Append(title)
	description := gtk.NewLabel("PKGER will validate your password once, then continue the operation without placing the password in any command.")
	description.AddCSSClass("subtitle")
	description.SetWrap(true)
	description.SetXAlign(0)
	card.Append(description)
	operation := gtk.NewLabel("Operation: " + label)
	operation.AddCSSClass("result-meta")
	operation.SetXAlign(0)
	card.Append(operation)
	password := gtk.NewEntry()
	password.SetPlaceholderText("Enter your user password")
	password.SetVisibility(false)
	password.SetInputPurpose(gtk.InputPurposePassword)
	password.SetHExpand(true)
	card.Append(password)
	note := gtk.NewLabel("The password is used only for sudo validation, then cleared. It is never saved, logged, or included in command arguments.")
	note.AddCSSClass("subtitle")
	note.SetWrap(true)
	note.SetXAlign(0)
	card.Append(note)
	errorLabel := gtk.NewLabel("")
	errorLabel.AddCSSClass("warning")
	errorLabel.SetWrap(true)
	errorLabel.SetXAlign(0)
	card.Append(errorLabel)
	content.Append(card)
	dialog.AddButton("Cancel", int(gtk.ResponseCancel))
	dialog.AddButton("Authenticate", int(gtk.ResponseOK))
	dialog.ConnectResponse(func(responseID int) {
		if responseID != int(gtk.ResponseOK) {
			g.authDialog = nil
			dialog.Close()
			g.endOperation()
			g.setBusy(false)
			g.setStatus("Operation cancelled")
			return
		}
		secret := []byte(password.Text())
		password.SetText("")
		if len(secret) == 0 {
			errorLabel.SetText("Password cannot be empty.")
			return
		}
		password.SetSensitive(false)
		errorLabel.SetText("Validating authorization…")
		go func() {
			ok, detail := validateSudo(secret)
			for i := range secret {
				secret[i] = 0
			}
			glib.IdleAdd(func() {
				if ok {
					g.authDialog = nil
					dialog.Close()
					g.runCommandsAsync(label, commands)
					return
				}
				password.SetSensitive(true)
				errorLabel.SetText(detail)
				g.setStatus("Authentication failed — try again")
			})
		}()
	})
	dialog.Present()
	password.GrabFocus()
}

func validateSudo(secret []byte) (bool, string) {
	input := append([]byte(nil), secret...)
	input = append(input, 10)
	cmd := exec.Command("sudo", "-S", "-p", "", "-v")
	cmd.Stdin = bytes.NewReader(input)
	cmd.Stdout = io.Discard
	var errOut strings.Builder
	cmd.Stderr = &errOut
	err := cmd.Run()
	for i := range input {
		input[i] = 0
	}
	if err == nil {
		return true, ""
	}
	detail := strings.TrimSpace(errOut.String())
	if detail == "" {
		detail = "Sudo authentication failed. Verify your password and try again."
	}
	return false, detail
}

func (g *GUIApp) runCommandsAsync(label string, commands []guiCommand) {
	go func() {
		var all strings.Builder
		tool := privTool()
		for _, command := range commands {
			cmdArgs := append([]string(nil), command.args...)
			if command.privileged && os.Geteuid() != 0 {
				if tool == "" {
					all.WriteString("No sudo or doas found.")
					all.WriteByte(10)
					continue
				}
				cmdArgs = append([]string{tool, "-n"}, cmdArgs...)
			}
			all.WriteString("$ ")
			all.WriteString(strings.Join(cmdArgs, " "))
			all.WriteByte(10)
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
			cmd := exec.CommandContext(ctx, cmdArgs[0], cmdArgs[1:]...)
			var out strings.Builder
			cmd.Stdout = io.MultiWriter(&out)
			cmd.Stderr = io.MultiWriter(&out)
			err := cmd.Run()
			timedOut := ctx.Err() == context.DeadlineExceeded
			cancel()
			text := out.String()
			all.WriteString(text)
			if timedOut {
				all.WriteString("ERROR: operation timed out after 20 minutes")
				all.WriteByte(10)
			} else if err != nil {
				all.WriteString(fmt.Sprintf("ERROR: %v", err))
				all.WriteByte(10)
			}
			all.WriteByte(10)
		}
		text := all.String()
		g.core.appendLog(text)
		glib.IdleAdd(func() {
			if g.packageOutput != nil {
				g.packageOutput.Buffer().SetText(text)
			}
			if g.repoOutput != nil {
				g.repoOutput.Buffer().SetText(text)
			}
			g.endOperation()
			g.setBusy(false)
			g.setStatus(label + " finished")
		})
	}()
}

func (g *GUIApp) loadUpdates(syncDB bool) {
	g.setBusy(true)
	go func() {
		g.core.loadUpdates(syncDB)
		glib.IdleAdd(func() {
			g.populateUpdates()
			g.updateHome()
			g.setBusy(false)
			g.setStatus(formatUpdateSummary(g.core.updates))
		})
	}()
}
func (g *GUIApp) populateUpdates() {
	if g.updateList == nil {
		return
	}
	g.updateList.RemoveAll()
	g.updateChecks = map[int]*gtk.CheckButton{}
	for i, u := range g.core.updates {
		row := gtk.NewListBoxRow()
		box := gtk.NewBox(gtk.OrientationHorizontal, 8)
		box.AddCSSClass("result-row")
		c := gtk.NewCheckButton()
		c.SetActive(u.Selected)
		idx := i
		c.ConnectToggled(func() { g.core.updates[idx].Selected = c.Active() })
		n := gtk.NewLabel(fmt.Sprintf("%s  %s → %s", u.Name, u.From, u.To))
		n.SetXAlign(0)
		n.SetHExpand(true)
		s := gtk.NewLabel(severity(u.Name, u.Repository, u.Source))
		s.AddCSSClass("result-meta")
		box.Append(c)
		box.Append(n)
		box.Append(s)
		row.SetChild(box)
		g.updateList.Append(row)
		g.updateChecks[i] = c
	}
}
func (g *GUIApp) setUpdateSelection(on bool) {
	for i := range g.core.updates {
		g.core.updates[i].Selected = on
		if c := g.updateChecks[i]; c != nil {
			c.SetActive(on)
		}
	}
}
func (g *GUIApp) selectSecurity() {
	for i, u := range g.core.updates {
		on := severity(u.Name, u.Repository, u.Source) == "Security"
		g.core.updates[i].Selected = on
		if c := g.updateChecks[i]; c != nil {
			c.SetActive(on)
		}
	}
}
func (g *GUIApp) applyUpdates() {
	var pac, flat []string
	for _, u := range g.core.updates {
		if u.Selected {
			if u.Source == "flatpak" {
				flat = append(flat, u.Name)
			} else {
				pac = append(pac, u.Name)
			}
		}
	}
	var commands []guiCommand
	if len(pac) > 0 {
		commands = append(commands, guiCommand{label: "Apply pacman updates", privileged: true, args: append([]string{"pacman", "-S", "--noconfirm"}, pac...)})
	}
	if len(flat) > 0 {
		commands = append(commands, guiCommand{label: "Apply Flatpak updates", args: append([]string{"flatpak", "update", "-y"}, flat...)})
	}
	g.executeCommands("Apply selected updates", commands)
}
func (g *GUIApp) refreshReposAsync() {
	g.setBusy(true)
	g.setStatus("Refreshing repositories…")
	go func() {
		g.core.loadRepos()
		glib.IdleAdd(func() { g.populateRepos(); g.setBusy(false); g.setStatus("Repositories refreshed") })
	}()
}
func (g *GUIApp) repoNames() []string {
	keys := make([]string, 0, len(g.core.repos))
	for k := range g.core.repos {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
func (g *GUIApp) filteredRepoNames() []string {
	q := ""
	if g.repoFilter != nil {
		q = strings.ToLower(strings.TrimSpace(g.repoFilter.Text()))
	}
	var out []string
	for _, k := range g.repoNames() {
		if q == "" || strings.Contains(strings.ToLower(k), q) {
			out = append(out, k)
		}
	}
	return out
}
func (g *GUIApp) populateRepos() {
	if g.repoList == nil {
		return
	}
	g.repoList.RemoveAll()
	for _, name := range g.filteredRepoNames() {
		row := gtk.NewListBoxRow()
		card := gtk.NewBox(gtk.OrientationVertical, 3)
		card.AddCSSClass("source-row")
		line := gtk.NewBox(gtk.OrientationHorizontal, 6)
		label := gtk.NewLabel(name)
		label.AddCSSClass("result-name")
		label.SetXAlign(0)
		label.SetHExpand(true)
		badge := gtk.NewLabel(classifyRepo(name))
		badge.AddCSSClass("badge")
		line.Append(label)
		line.Append(badge)
		card.Append(line)
		count := gtk.NewLabel(fmt.Sprintf("%d available packages", len(g.core.repos[name])))
		count.AddCSSClass("result-meta")
		count.SetXAlign(0)
		card.Append(count)
		row.SetChild(card)
		g.repoList.Append(row)
	}
	if g.selectedRepo != "" {
		g.populateRepoPackages()
	}
}
func (g *GUIApp) populateRepoPackages() {
	if g.repoPackages == nil {
		return
	}
	list := append([]RepoPackage(nil), g.core.repos[g.selectedRepo]...)
	filter := ""
	if g.repoPackageFilter != nil {
		filter = strings.ToLower(strings.TrimSpace(g.repoPackageFilter.Text()))
	}
	out := list[:0]
	installed := 0
	for _, p := range list {
		if p.Installed {
			installed++
		}
		if filter != "" && !strings.Contains(strings.ToLower(p.Name+" "+p.Version), filter) {
			continue
		}
		if g.repoInstalledOnly != nil && g.repoInstalledOnly.Active() && !p.Installed {
			continue
		}
		out = append(out, p)
	}
	list = out
	key := int(g.repoSortDrop.Selected())
	sort.SliceStable(list, func(i, j int) bool {
		switch key {
		case 1:
			return list[i].Version < list[j].Version
		case 3:
			if list[i].Installed != list[j].Installed {
				return list[i].Installed
			}
			return list[i].Name < list[j].Name
		default:
			return strings.ToLower(list[i].Name) < strings.ToLower(list[j].Name)
		}
	})
	g.repoVisiblePkgs = list
	g.repoPackages.RemoveAll()
	g.repoPackageChecks = map[string]*gtk.CheckButton{}
	for _, p := range list {
		row := gtk.NewListBoxRow()
		box := gtk.NewBox(gtk.OrientationHorizontal, 8)
		box.AddCSSClass("result-row")
		check := gtk.NewCheckButton()
		check.SetActive(g.repoSelected[p.Name])
		pkg := p
		check.ConnectToggled(func() {
			if check.Active() {
				g.repoSelected[pkg.Name] = true
			} else {
				delete(g.repoSelected, pkg.Name)
			}
		})
		name := gtk.NewLabel(p.Name)
		name.AddCSSClass("result-name")
		name.SetXAlign(0)
		name.SetHExpand(true)
		meta := gtk.NewLabel(fmt.Sprintf("%s · %s", p.Version, yesNo(p.Installed)))
		meta.AddCSSClass("result-meta")
		box.Append(check)
		box.Append(name)
		box.Append(meta)
		row.SetChild(box)
		g.repoPackages.Append(row)
		g.repoPackageChecks[p.Name] = check
	}
	if g.repoStats != nil {
		g.repoStats.SetText(fmt.Sprintf("%d shown · %d installed", len(list), installed))
	}
}
func (g *GUIApp) setRepoSelection(on bool) {
	if g.repoSelected == nil {
		g.repoSelected = map[string]bool{}
	}
	for _, p := range g.repoVisiblePkgs {
		if on {
			g.repoSelected[p.Name] = true
		} else {
			delete(g.repoSelected, p.Name)
		}
		if c := g.repoPackageChecks[p.Name]; c != nil {
			c.SetActive(on)
		}
	}
	g.populateRepoPackages()
}
func (g *GUIApp) showRepoPackage(p RepoPackage) {
	g.repoDetails.Buffer().SetText(fmt.Sprintf("Name: %s\nRepository: %s\nVersion: %s\nInstalled: %v\n\nLoading metadata…", p.Name, g.selectedRepo, p.Version, p.Installed))
	go func() {
		out, _, _ := runCapture(35*time.Second, "pacman", "-Si", p.Name)
		if out == "" {
			out, _, _ = runCapture(25*time.Second, "pacman", "-Qi", p.Name)
		}
		if out == "" {
			out = "No additional metadata available."
		}
		text := fmt.Sprintf("Name: %s\nRepository: %s\nVersion: %s\nInstalled: %v\n\n%s", p.Name, g.selectedRepo, p.Version, p.Installed, out)
		glib.IdleAdd(func() { g.repoDetails.Buffer().SetText(text) })
	}()
}
func (g *GUIApp) installRepoSelected() {
	var names []string
	for _, p := range g.repoVisiblePkgs {
		if g.repoSelected[p.Name] && !p.Installed {
			names = append(names, p.Name)
		}
	}
	if len(names) == 0 {
		g.setStatus("Select one or more not-installed packages")
		return
	}
	g.executeCommands("Install repository packages", []guiCommand{{label: "Install repository packages", privileged: true, args: append([]string{"pacman", "-S", "--noconfirm"}, names...)}})
}
func (g *GUIApp) removeRepoSelected() {
	var names []string
	for _, p := range g.repoVisiblePkgs {
		if g.repoSelected[p.Name] && p.Installed {
			names = append(names, p.Name)
		}
	}
	if len(names) == 0 {
		g.setStatus("Select one or more installed packages")
		return
	}
	g.executeCommands("Remove repository packages", []guiCommand{{label: "Remove repository packages", privileged: true, args: append([]string{"pacman", "-Rns", "--noconfirm"}, names...)}})
}
func (g *GUIApp) scanImages() {
	g.setBusy(true)
	g.setStatus("Scanning AppImage locations…")
	go func() {
		g.core.scanAppImages()
		glib.IdleAdd(func() {
			g.populateImages()
			g.setBusy(false)
			g.setStatus(fmt.Sprintf("Found %d AppImages", len(g.core.appImages)))
		})
	}()
}
func (g *GUIApp) populateImages() {
	if g.imageList == nil {
		return
	}
	g.imageList.RemoveAll()
	for _, p := range g.core.appImages {
		row := gtk.NewListBoxRow()
		row.SetChild(gtk.NewLabel(p.Name + "  —  " + p.Path))
		g.imageList.Append(row)
	}
}
func (g *GUIApp) showImage() {
	if g.selectedImage < 0 || g.selectedImage >= len(g.core.appImages) {
		return
	}
	p := g.core.appImages[g.selectedImage]
	g.imageDetails.Buffer().SetText(fmt.Sprintf("Name: %s\nType: AppImage\nPath: %s", p.Name, p.Path))
}
func (g *GUIApp) launchImage() {
	if g.selectedImage < 0 || g.selectedImage >= len(g.core.appImages) {
		g.setStatus("Select an AppImage first")
		return
	}
	p := g.core.appImages[g.selectedImage]
	_ = os.Chmod(p.Path, 0755)
	if err := exec.Command(p.Path).Start(); err != nil {
		g.setStatus("Launch failed: " + err.Error())
	} else {
		g.setStatus("AppImage launched")
	}
}
func (g *GUIApp) removeImage() {
	if g.selectedImage < 0 || g.selectedImage >= len(g.core.appImages) {
		g.setStatus("Select an AppImage first")
		return
	}
	p := g.core.appImages[g.selectedImage]
	if os.Remove(p.Path) == nil {
		g.scanImages()
	} else {
		g.setStatus("Could not remove AppImage")
	}
}
func (g *GUIApp) runTool(name string) {
	g.switchPage("logs")
	var args []string
	priv := false
	switch name {
	case "upgrade":
		args = []string{"pacman", "-Syu", "--noconfirm"}
		priv = true
	case "sync":
		args = []string{"pacman", "-Sy", "--noconfirm"}
		priv = true
	case "clean":
		args = []string{"pacman", "-Sc", "--noconfirm"}
		priv = true
	case "deepclean":
		args = []string{"pacman", "-Scc", "--noconfirm"}
		priv = true
	case "filedb":
		args = []string{"pacman", "-Fy", "--noconfirm"}
		priv = true
	case "orphans":
		args = []string{"bash", "-lc", "pacman -Qtdq | pacman -Rns --noconfirm -"}
		priv = true
	case "aur-upgrade":
		if h := aurHelper(); h != "" {
			args = []string{h, "-Syu", "--noconfirm"}
		}
	case "verify":
		args = []string{"pacman", "-Qk"}
	case "db-check":
		args = []string{"pacman", "-Dk"}
	case "mirrors":
		args = []string{"reflector", "--latest", "20", "--sort", "rate", "--save", "/etc/pacman.d/mirrorlist"}
		priv = true
	case "keyring":
		args = []string{"pacman-key", "--refresh-keys"}
		priv = true
	case "paccache":
		args = []string{"paccache", "-r", "-k", "2"}
		priv = true
	case "daemon-reload":
		args = []string{"systemctl", "daemon-reload"}
		priv = true
	case "user-daemon-reload":
		args = []string{"systemctl", "--user", "daemon-reload"}
	case "mkinitcpio":
		args = []string{"mkinitcpio", "-P"}
		priv = true
	case "grub":
		args = []string{"grub-mkconfig", "-o", "/boot/grub/grub.cfg"}
		priv = true
	case "backup-db":
		args = []string{"tar", "-cJf", "pacman-local-db.tar.xz", "-C", "/var/lib/pacman", "local"}
		priv = true
	case "export-all":
		g.core.exportCommand("pacman -Q", false, "pacman", "-Q")
		g.refreshLogs()
		return
	case "export-native":
		g.core.exportCommand("pacman -Qne", false, "pacman", "-Qne")
		g.refreshLogs()
		return
	case "pacman-log":
		g.core.loadPacmanLog()
		g.refreshLogs()
		return
	case "checkupdates":
		args = []string{"checkupdates"}
	case "foreign":
		args = []string{"pacman", "-Qm"}
	case "native-explicit":
		args = []string{"pacman", "-Qne"}
	case "failed-services":
		args = []string{"systemctl", "--failed", "--no-pager", "--no-legend"}
	case "journal-errors":
		args = []string{"journalctl", "-p", "err", "-n", "120", "--no-pager"}
	case "pacnew":
		args = []string{"bash", "-lc", "find /etc -name '*.pacnew' 2>/dev/null | sort"}
	case "ip":
		args = []string{"ip", "-br", "a"}
	case "uname":
		args = []string{"uname", "-a"}
	case "df":
		args = []string{"df", "-h", "/", "/var", "/home"}
	case "memory":
		args = []string{"free", "-h"}
	case "pacdiff":
		args = []string{"pacdiff", "--safe"}
	case "pkgfile":
		args = []string{"pkgfile", "-u"}
		priv = true
	case "flatpak-repair":
		args = []string{"flatpak", "repair", "--noninteractive"}
	case "flatpak-update":
		args = []string{"flatpak", "update", "-y"}
	case "flatpak-unused":
		args = []string{"flatpak", "uninstall", "--unused", "-y"}
	default:
		return
	}
	if len(args) > 0 {
		if name == "aur-upgrade" {
			g.executeCommands(name, []guiCommand{{label: name, authRequired: true, args: args}})
			return
		}
		g.executeGUI(name, priv, args...)
	}
}
func (g *GUIApp) refreshLogs() {
	if g.logView != nil {
		g.logView.Buffer().SetText(strings.Join(g.core.logLines, "\n"))
	}
}
func (g *GUIApp) loadNews() {
	if g.newsView == nil {
		return
	}
	g.setStatus("Loading Arch Linux news…")
	go func() {
		items := fetchNews(20)
		var b strings.Builder
		for _, n := range items {
			b.WriteString(n.Title + "\n" + n.Date + "\n" + n.Link + "\n" + truncate(stripHTML(n.Summary), 400) + "\n\n")
		}
		if b.Len() == 0 {
			b.WriteString("No news items available.")
		}
		text := b.String()
		glib.IdleAdd(func() { g.newsView.Buffer().SetText(text); g.setStatus("News loaded") })
	}()
}
func (g *GUIApp) saveGUISettings() {
	for key, c := range g.settingsChecks {
		switch key {
		case "auto_check_updates":
			g.core.settings.AutoCheckUpdates = c.Active()
		case "show_security_alerts":
			g.core.settings.ShowSecurity = c.Active()
		case "confirm_install":
			g.core.settings.ConfirmInstall = c.Active()
		case "confirm_remove":
			g.core.settings.ConfirmRemove = c.Active()
		case "search_official":
			g.core.settings.SearchOfficial = c.Active()
		case "search_aur":
			g.core.settings.SearchAUR = c.Active()
		case "search_flatpak":
			g.core.settings.SearchFlatpak = c.Active()
		case "search_appimage":
			g.core.settings.SearchAppImage = c.Active()
		}
	}
	g.core.saveSettings()
	g.setStatus("Settings saved")
}
func (g *GUIApp) refreshDashboard() { g.updateHome() }
func yesNo(v bool) string {
	if v {
		return "Installed"
	}
	return "Not installed"
}

func newScrolledWindowWithChild(child gtk.Widgetter) *gtk.ScrolledWindow {
	sw := gtk.NewScrolledWindow()
	sw.SetChild(child)
	return sw
}
