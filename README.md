<div align="center">

# 🗃️ PKGER

**A modern, feature-rich GTK package manager for Arch Linux**


</div>

---

<p align="center">

  <img src="https://img.shields.io/badge/PKGER-v1.2.2-informational?style=for-the-badge"/>
  &nbsp;
  <img src="https://img.shields.io/badge/Arch%20Linux-GTK%204-1793D1?style=for-the-badge&logo=arch-linux&logoColor=white"/>
  &nbsp;
  <img src="https://img.shields.io/badge/Built%20with-Go-00ADD8?style=for-the-badge&logo=go&logoColor=white"/>
  &nbsp;

</p>

[![Arch](https://img.shields.io/badge/Download-pkg.tar.zst-1793D1?style=for-the-badge&logo=arch-linux&logoColor=blue)](https://github.com/almezali/pkger-g/releases/download/v1.2.2/pkger-1.2.2-4-x86_64.pkg.tar.zst)
[![AppImage](https://img.shields.io/badge/Download-AppImage-5C5C5C?style=for-the-badge&logo=linux&logoColor=yellow)](https://github.com/almezali/pkger-g/releases/download/v1.2.2/Pkger-1.2.2-4-x86_64.AppImage)
[![GitHub Downloads](https://img.shields.io/github/downloads/almezali/pkger-g/total?style=for-the-badge&logo=github&label=Downloads&color=blue)](https://github.com/almezali/pkger-g/releases)
[![Latest Release Downloads](https://img.shields.io/github/downloads/almezali/pkger-g/latest/total?style=for-the-badge&label=Latest%20Downloads)](https://github.com/almezali/pkger-g/releases/latest)

---

## 📖 About

**PKGER** A clean GTK 4 package manager for Arch Linux. Manage official repos, AUR, Flatpak and AppImage, track updates and system health live, and install local packages, in one clean interface.

---

## 📥 Installation

### From the AUR

```bash
yay -S pkger-bin
```

### From the release package

Download the latest `.pkg.tar.zst` from the [Releases page](https://github.com/almezali/pkger-g/releases) and install it with pacman:

```bash
sudo pacman -U pkger-1.2.2-4-x86_64.pkg.tar.zst
```

---

## ✨ Features

### 🖥️ Home Screen

The Home screen shows live information about your system:

| Item | What it shows |
|---|---|
| 🩺 System health | Quick check on how your system is doing |
| 📦 Installed packages | Total number of packages on your system |
| 🔄 Updates waiting | Includes a separate count for security updates |
| 🗑️ Cache size | How much space Pacman's cache is using |
| 🧬 Kernel version | Your current kernel, at a glance |
| 🌐 Repositories | How many repos are set up |
| ⏱️ Uptime & load | How long your system has been running, and how busy it is |
| 💾 Disk & memory use | Live storage and RAM usage |
| 🕓 Last update time | When the info was last refreshed |

### 📦 Package Management

- Choose where packages come from:
  - Official repos
  - AUR
  - Already installed packages
  - Flatpak
  - AppImage
  - Developer packages
  - Search across everything at once
- Sort and filter by name, repo, version, or installed status.
- Select multiple packages with checkboxes, plus **Select All** and **Clear Selection** buttons.
- Details and action-result panels for every package.
- Install or remove several packages in one go.
- Pick multiple local package files with the built-in file browser:
  - `.pkg.tar.zst`
  - `.pkg.tar.xz`
  - `.pkg.tar.gz`
- Install several local files together using `pacman -U`.

### 🧩 AUR Support

- Works with `yay` or `paru`, detected automatically.
- AUR installs behave the same way as other package sources.
- Admin permission is requested right when an AUR install starts.
- AUR tools run as your normal user, while system changes use a separate, verified admin session.
- Clear messages if neither `yay` nor `paru` is found.
- Improved error messages and output when something goes wrong.

### 🌐 Sources & Repositories

- Clean card-based Sources page.
- Repos grouped into categories, with package counts.
- Filter packages inside a repo, including an **Installed Only** filter.
- Sort and multi-select packages inside a repo.
- Package details shown below the list.
- Install or remove packages straight from the repo view.
- Repo statistics with export support.

### 🔄 Updates & System Tools

- Update checks run in the background.
- Update everything, or just security updates.
- Sync and refresh with one click.
- System maintenance and diagnostic tools.
- Live command output with all logs in one place.
- AppImage scanning and management.
- Arch Linux news without freezing the app.

---

## 🔐 Security

- Admin login window built with GTK4.
- Password is hidden while typing and cleared right after verification.
- Passwords are never saved, logged, passed as command arguments, or stored anywhere.
- Sudo access is verified through standard input with `sudo -S -v`.
- Package actions then run with `sudo -n`, relying on the temporary sudo session.
- Doas actions use `doas -n` and require existing permission or a saved policy.
- Admin approval is requested whenever install, remove, or update actions need it.

---

## ⚙️ Stability & Performance

- Package actions, AUR commands, repo refreshes, searches, update checks, data loading, news fetching, and AppImage scans all run in the background.
- The interface stays responsive during long tasks.
- Two package actions can't accidentally run at the same time, and you get a clear message if one is already running.
- A time limit stops commands from hanging forever.
- Better error handling and clearer output overall.

---

## 🖼️ Screenshots

<p align="center">
  <img width="1366" height="768" alt="PKGER screenshot 1" src="https://github.com/user-attachments/assets/18299fa1-0a62-4849-bdb2-a7fae5ca9cf3" />
  <img width="1366" height="768" alt="PKGER screenshot 2" src="https://github.com/user-attachments/assets/9c8e44bb-a16f-4a4b-937b-4711c84fd3a5" />
  <img width="1366" height="768" alt="PKGER screenshot 3" src="https://github.com/user-attachments/assets/c379076f-4cd3-4326-a1f7-f4cc8008af5c" />
  <img width="1366" height="768" alt="PKGER screenshot 4" src="https://github.com/user-attachments/assets/a975d444-fab9-45a0-a88b-cfd86224af2e" />
  <img width="1366" height="768" alt="PKGER screenshot 5" src="https://github.com/user-attachments/assets/6d60eda6-01d9-4d01-8548-f160e3b7ff44" />
</p>

---

## 🐞 Issues & Feedback

Found a bug or have an idea? Please [open an issue](https://github.com/almezali/pkger-g/issues).

---

<div align="center">

Made with ❤️ by [almezali](https://github.com/almezali)

</div>
