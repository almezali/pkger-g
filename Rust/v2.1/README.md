# PKGER v2.1 (Rust + GTK4)

Arch Linux package manager GUI — a feature-complete Rust port of the original Go/GTK4 PKGER application.

## Requirements

- Rust toolchain (`cargo`, `rustc`)
- GTK 4 development libraries (`gtk4`, `glib`, `gio`)
- Arch Linux (or Arch-based) with `pacman` available

On Arch:

```bash
sudo pacman -S --needed gtk4 base-devel
```

## Build

```bash
cargo build --release
```

The binary is written to `target/release/pkger`.

## Run

```bash
./target/release/pkger
```

Options:

- `--version` / `-V` — print version
- `--help` — show help

## Configuration

Settings, history, and favorites are stored under:

- `~/.config/pkger/settings.json`
- `~/.config/pkger/history.json`
- `~/.config/pkger/favorites.json`

## Application ID

`com.almezali.pkger.gtk4`
