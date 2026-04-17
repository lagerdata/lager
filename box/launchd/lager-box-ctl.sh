#!/bin/bash
# lager-box-ctl.sh — thin wrapper around launchctl for the macOS Lager box.
#
# Usage:
#   sudo ./lager-box-ctl.sh install        # bootstrap the LaunchDaemon
#   sudo ./lager-box-ctl.sh uninstall      # bootout and remove the plist
#   sudo ./lager-box-ctl.sh start          # kickstart (start now if loaded)
#   sudo ./lager-box-ctl.sh stop           # stop the daemon (it will restart per KeepAlive unless uninstalled)
#   sudo ./lager-box-ctl.sh restart        # kickstart -k (force restart)
#   sudo ./lager-box-ctl.sh status         # print launchctl print output
#
# This wrapper exists so the install scripts and human operators don't have
# to remember the exact launchctl invocation syntax (which differs between
# `launchctl load` and the modern `bootstrap`/`bootout`/`kickstart` verbs).

set -euo pipefail

LABEL="com.lager.box"
SERVICE_TARGET="system/${LABEL}"
PLIST_DEST="/Library/LaunchDaemons/${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SOURCE="${SCRIPT_DIR}/${LABEL}.plist"

require_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "ERROR: $1 requires root. Re-run with sudo." >&2
        exit 1
    fi
}

cmd_install() {
    require_root "install"
    if [ ! -f "$PLIST_SOURCE" ]; then
        echo "ERROR: plist source not found: $PLIST_SOURCE" >&2
        exit 1
    fi

    install -o root -g wheel -m 0644 "$PLIST_SOURCE" "$PLIST_DEST"
    echo "Installed plist: $PLIST_DEST"

    # bootout first in case a stale entry exists from a previous install
    launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true
    launchctl bootstrap system "$PLIST_DEST"
    echo "Bootstrapped: $SERVICE_TARGET"
    launchctl enable "$SERVICE_TARGET"
    launchctl kickstart -k "$SERVICE_TARGET"
    echo "Kickstarted: $SERVICE_TARGET"
}

cmd_uninstall() {
    require_root "uninstall"
    launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true
    if [ -f "$PLIST_DEST" ]; then
        rm -f "$PLIST_DEST"
        echo "Removed plist: $PLIST_DEST"
    fi
    echo "Uninstalled: $SERVICE_TARGET"
}

cmd_start() {
    require_root "start"
    launchctl kickstart "$SERVICE_TARGET"
}

cmd_stop() {
    require_root "stop"
    launchctl kill SIGTERM "$SERVICE_TARGET" || true
}

cmd_restart() {
    require_root "restart"
    launchctl kickstart -k "$SERVICE_TARGET"
}

cmd_status() {
    launchctl print "$SERVICE_TARGET" 2>/dev/null || {
        echo "Service $SERVICE_TARGET is not loaded."
        return 1
    }
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    "")
        echo "Usage: $0 {install|uninstall|start|stop|restart|status}" >&2
        exit 1
        ;;
    *)
        echo "Unknown command: $1" >&2
        echo "Usage: $0 {install|uninstall|start|stop|restart|status}" >&2
        exit 1
        ;;
esac
