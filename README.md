# PKGER - Professional Package Manager

**A modern, feature-rich GTK package manager for Arch Linux**

---

[![Version](https://img.shields.io/badge/version-1.1-blue?style=for-the-badge&logo=linux)](https://github.com/almezali/pkger-g/releases/tag/v1.1)
[![GTK](https://img.shields.io/badge/GTK-4.0-green?style=for-the-badge&logo=gnome)](https://gtk.org)
[![Python](https://img.shields.io/badge/Python-3.13+-yellow?style=for-the-badge&logo=python)](https://python.org)
[![Arch Linux](https://img.shields.io/badge/Arch%20Linux-Linux-1793D1?style=for-the-badge&logo=arch-linux&logoColor=white)](https://archlinux.org)

[![Arch](https://img.shields.io/badge/Download-pkg.tar.zst-1793D1?style=for-the-badge&logo=arch-linux&logoColor=white)](https://github.com/almezali/pkger-g/releases/download/v1.1/pkger-1.1-1-x86_64.pkg.tar.zst)
[![AppImage](https://img.shields.io/badge/Download-AppImage-5C5C5C?style=for-the-badge&logo=linux&logoColor=white)](https://github.com/almezali/pkger-g/releases/download/v1.1/Pkger-x86_64.AppImage)

---

## ✨ Features

### 🎯 Core Functionality
- **Multi-Repository Support**: Seamlessly manage packages from official Arch repositories and AUR
- **Advanced Search**: Fast, comprehensive package search across all repositories
- **Smart Installation**: Automated dependency resolution and conflict handling
- **System Updates**: One-click system-wide updates with progress tracking
- **Local Package Support**: Install local `.pkg.tar.zst` and `.pkg.tar.xz` files
- **Orphan Management**: Identify and remove unused packages automatically

### 🖥️ User Interface
- **Modern GTK Design**: Clean, responsive interface that follows system theme
- **Tabbed Navigation**: Organized workspace with Packages, Details, Repositories, and Updates tabs
- **Real-time Output**: Live command output with timestamps and progress indicators
- **Detailed Package Info**: Comprehensive package details including dependencies and reverse dependencies
- **Batch Operations**: Select and manage multiple packages simultaneously

### 🔧 Advanced Tools
- **Repository Browser**: Explore packages by repository with filtering options
- **Update Manager**: Visual update management with selective installation
- **Cache Management**: Intelligent package cache cleaning
- **Dependency Tracker**: Visualize package relationships and dependencies
- **Export Functions**: Export package lists and details for backup or documentation

### 🔒 Security & Reliability
- **Secure Authentication**: Safe sudo password handling
- **Operation Validation**: Confirmation dialogs for critical operations
- **Error Handling**: Robust error recovery and user feedback
- **Background Processing**: Non-blocking operations with progress tracking

## 📸 Screenshots

<div align="center">

### light Interface
![light Interface](https://github.com/almezali/pkger-g/blob/main/Screenshot-n1.jpg)
*Clean, intuitive package management interface*

### dark Interface
![dark Interface](https://github.com/almezali/pkger-g/blob/main/Screenshot-n2.jpg)
*Comprehensive package information and repository exploration*

</div>

## 🚀 Installation

### From AUR (Recommended)
```bash
yay -S pkger-g
```

### Manual Installation
```bash
git clone https://github.com/almezali/pkger-g.git
cd pkger-g
chmod +x pkger-g.py
sudo cp pkger-g.py /usr/local/bin/pkger-g
```
### Run
```bash
pkger-g
```
### Direct Run
```bash
python3 pkger_g.py
```
## 📋 System Requirements

### Required Dependencies
- **Python**: 3.6 or newer
- **PyGObject**: Python GTK bindings
- **GTK**: 3.0 or newer
- **pacman**: Arch Linux package manager
- **sudo**: Administrative privileges support

### Optional Dependencies
- **yay**: AUR package management (recommended)
- **pactree**: Dependency tree visualization
- **xdg-utils**: Homepage opening functionality

### Installation Commands
```bash
# Install required dependencies
sudo pacman -S python python-gobject gtk3

# Install optional dependencies
sudo pacman -S pacman-contrib  # for pactree
yay -S yay                     # for AUR support
```

## 🎮 Usage

### Launch Application
```bash
pkger-g
```

### Quick Start Guide

1. **Search Packages**: Use the search bar to find packages across repositories
2. **Browse by Repository**: Switch to the Repositories tab to explore packages by source
3. **Install Packages**: Select packages and click Install (requires sudo password)
4. **System Updates**: Use the Updates tab to manage system-wide updates
5. **Local Packages**: Use "Browse PKG" to install local package files

### Key Features Walkthrough

#### Package Management
- Search across Official, AUR, and Installed packages
- View detailed package information including dependencies
- Install, remove, or reinstall packages with one click
- Handle local package files with drag-and-drop support

#### System Maintenance
- Check for and apply system updates selectively
- Clean package cache to free disk space
- Remove orphaned packages automatically
- Fix broken dependencies with built-in recovery tools

#### Advanced Operations
- Export package lists for backup or documentation
- Copy package details to clipboard
- Open package homepages directly from the interface
- Filter and sort packages by various criteria

## 🏗️ Architecture

PKGER is built with modern Python and GTK technologies:

- **Frontend**: GTK 3.0 with PyGObject bindings
- **Backend**: Native pacman/yay integration
- **Threading**: Asynchronous operations for responsive UI
- **Caching**: Intelligent package information caching
- **Security**: Safe credential handling and operation validation


### Development Setup
```bash
git clone https://github.com/almezali/pkger-g.git
cd pkger-g
python -m venv venv
source venv/bin/activate
pip install -r requirements-dev.txt
```

### Report Issues
Found a bug or have a feature request? Please create an issue on our [GitHub Issues](https://github.com/almezali/pkger-g/issues) page.

## 📜 License

This project is licensed under the GPL-3.0 License.

## 👨‍💻 Author

**almezali** - *Developer and Maintainer*

## 🙏 Acknowledgments

- Arch Linux community for the robust package management system
- GTK developers for the excellent UI toolkit
- PyGObject maintainers for Python bindings
- AUR helpers developers (especially yay team)

---

<div align="center">

**Made with ❤️ for the Arch Linux community**

[Report Bug](https://github.com/almezali/pkger-g/issues) • [Request Feature](https://github.com/almezali/pkger-g/issues) • [Documentation](https://github.com/almezali/pkger-g/wiki)

</div>
