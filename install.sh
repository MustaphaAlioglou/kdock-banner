#!/usr/bin/env bash
set -euo pipefail

APP_ID="kdock-banner"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="user"
ACTION="install"
PREFIX=""

usage() {
    cat <<EOF
Usage: ./install.sh [options]

  (no options)     Install for the current user into ~/.local
  --system         Install for all users into /usr/local (uses sudo)
  --prefix DIR     Install into a custom prefix
  --uninstall      Remove an installation (combine with --system/--prefix if used)
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --system) MODE="system" ;;
        --prefix) PREFIX="${2:?--prefix needs a directory}"; MODE="custom"; shift ;;
        --uninstall) ACTION="uninstall" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

case "$MODE" in
    user) PREFIX="$HOME/.local" ;;
    system) PREFIX="/usr/local" ;;
esac

SUDO=""
if [[ ! -w "$(dirname "$PREFIX")" && ! -w "$PREFIX" ]] || [[ "$MODE" == "system" && $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

LIBDIR="$PREFIX/share/$APP_ID"
BIN="$PREFIX/bin/$APP_ID"
DESKTOP="$PREFIX/share/applications/$APP_ID.desktop"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"

info() { printf '\033[1;34m::\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

find_python() {
    local c
    for c in /usr/bin/python3 python3 python; do
        if command -v "$c" >/dev/null && "$c" -c "import PySide6.QtWidgets" 2>/dev/null; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

pyside_hint() {
    local id=""
    [[ -r /etc/os-release ]] && id="$(. /etc/os-release; echo "${ID} ${ID_LIKE:-}")"
    case "$id" in
        *arch*) echo "sudo pacman -S pyside6" ;;
        *fedora*) echo "sudo dnf install python3-pyside6" ;;
        *suse*) echo "sudo zypper install python3-pyside6" ;;
        *debian*|*ubuntu*) echo "sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore" ;;
        *) echo "pip install --user PySide6" ;;
    esac
}

refresh_caches() {
    command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && $SUDO gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true
    command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
}

install_app() {
    PY="$(find_python)" || die "PySide6 (Qt for Python) is required. Install it with: $(pyside_hint)"
    info "Using Python: $PY"

    for cmd in plasma-apply-desktoptheme kreadconfig6; do
        command -v "$cmd" >/dev/null || warn "'$cmd' not found - Dock Banner needs KDE Plasma 6."
    done
    if ! command -v qdbus6 >/dev/null && ! command -v qdbus-qt6 >/dev/null \
        && [[ ! -x /usr/lib/qt6/bin/qdbus && ! -x /usr/lib64/qt6/bin/qdbus ]]; then
        warn "qdbus (Qt 6) not found - panel size detection will not work, you can still enter it manually."
    fi

    info "Installing to $PREFIX"
    $SUDO install -Dm644 "$SRC/kdock_banner.py" "$LIBDIR/kdock_banner.py"
    $SUDO install -Dm644 "$SRC/data/$APP_ID.svg" "$LIBDIR/data/$APP_ID.svg"
    $SUDO install -Dm644 "$SRC/data/$APP_ID.svg" "$ICON"

    local tmp
    tmp="$(mktemp -d)"
    printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$PY" "$LIBDIR/kdock_banner.py" > "$tmp/bin"
    sed "s|@BIN@|$BIN|" "$SRC/data/$APP_ID.desktop" > "$tmp/desktop"
    $SUDO install -Dm755 "$tmp/bin" "$BIN"
    $SUDO install -Dm644 "$tmp/desktop" "$DESKTOP"
    rm -rf "$tmp"

    refresh_caches
    info "Done. Launch \"Dock Banner\" from the app menu or run: $APP_ID"
    [[ ":$PATH:" == *":$PREFIX/bin:"* ]] || warn "$PREFIX/bin is not in your PATH (the app menu entry still works)."
}

uninstall_app() {
    if [[ -x "$BIN" ]]; then
        info "Restoring your original Plasma theme"
        "$BIN" --restore || true
    fi
    info "Removing files from $PREFIX"
    $SUDO rm -f "$BIN" "$DESKTOP" "$ICON"
    $SUDO rm -rf "$LIBDIR"
    refresh_caches
    info "Uninstalled. Your settings remain in ~/.config/kdockbanner.json (delete it to reset)."
}

if [[ "$ACTION" == "install" ]]; then
    install_app
else
    uninstall_app
fi
