#!/bin/bash
# Development launcher for a Box on macOS: native host agent + Box container.
#
# This is a developer tool for working on the macOS port. It is NOT how users
# install Lager -- that is `lager install`, which will provision the same two
# pieces. Nothing here runs on, or changes, a Linux Box.
#
#   tools/macos_dev_box.sh up       start the agent and the container
#   tools/macos_dev_box.sh down     stop both
#   tools/macos_dev_box.sh status   show what is running and the link state
#   tools/macos_dev_box.sh logs     tail the agent and relay logs
#
# The container runs the published Box image with this checkout's box/lager
# mounted over the image's copy, so edits take effect on the next `up`.
# Published ports are bound to 127.0.0.1: the Box API is unauthenticated by
# design, and a development laptop should not offer it to the network.
#
# Environment:
#   LAGER_DEV_DIR      state directory   (default ~/Library/Application Support/Lager-dev)
#   LAGER_DEV_IMAGE    Box image         (default ghcr.io/lagerdata/lager-box:v<cli version>)
#   LAGER_DEV_PLATFORM image platform    (default linux/amd64, the only one published today)
#   LAGER_DEV_PYTHON   Python for the agent (default <repo>/.venv/bin/python)
#   LAGER_DEV_NAME     container name    (default lager-macos-dev)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_DIR="${LAGER_DEV_DIR:-$HOME/Library/Application Support/Lager-dev}"
ETC_DIR="$DEV_DIR/etc"
LOG_DIR="$DEV_DIR/logs"
PID_FILE="$DEV_DIR/agent.pid"
NAME="${LAGER_DEV_NAME:-lager-macos-dev}"
PLATFORM="${LAGER_DEV_PLATFORM:-linux/amd64}"
PYTHON="${LAGER_DEV_PYTHON:-$REPO/.venv/bin/python}"

# The script-execution service is published on 8301 because macOS's AirPlay
# receiver holds 5000. 8301 -> 5000 is the same alias a Linux Box publishes.
PYTHON_SERVICE_HOST_PORT=8301

cli_version() {
    sed -n "s/^__version__ *= *['\"]\([^'\"]*\)['\"].*/\1/p" "$REPO/cli/__init__.py" | head -1
}
IMAGE="${LAGER_DEV_IMAGE:-ghcr.io/lagerdata/lager-box:v$(cli_version)}"

die() { echo "error: $*" >&2; exit 1; }

agent_pid() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE")"
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

cmd_up() {
    [ "$(uname -s)" = "Darwin" ] || die "this launcher is for macOS"
    [ -x "$PYTHON" ] || die "no Python at $PYTHON (set LAGER_DEV_PYTHON)"
    docker info >/dev/null 2>&1 || die "Docker is not running"

    mkdir -p "$LOG_DIR"
    mkdir -p -m 700 "$ETC_DIR"
    [ -f "$ETC_DIR/saved_nets.json" ] || echo '[]' > "$ETC_DIR/saved_nets.json"

    if agent_pid >/dev/null; then
        echo "Host agent already running (pid $(agent_pid))"
    else
        PYTHONPATH="$REPO/box" nohup "$PYTHON" -m lager.host run --config-dir "$ETC_DIR" \
            >> "$LOG_DIR/agent.log" 2>&1 &
        echo $! > "$PID_FILE"
        echo "Started host agent (pid $(cat "$PID_FILE"), log $LOG_DIR/agent.log)"
    fi
    # The endpoint record must exist before the container reads it.
    for _ in $(seq 1 50); do
        [ -f "$ETC_DIR/host_agent.json" ] && break
        sleep 0.1
    done
    [ -f "$ETC_DIR/host_agent.json" ] || die "host agent did not write its endpoint record"

    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # No --privileged, no /dev, no /sys, no dbus: this container has no route
    # to hardware except the host agent.
    docker run -d --name "$NAME" --platform "$PLATFORM" \
        -v "$REPO/box/lager:/app/lager/lager:ro" \
        -v "$ETC_DIR:/etc/lager" \
        -p 127.0.0.1:9000:9000 \
        -p "127.0.0.1:${PYTHON_SERVICE_HOST_PORT}:5000" \
        -p 127.0.0.1:8100:8100 \
        "$IMAGE" /app/lager/lager/docker/start-services-hostlink.sh >/dev/null
    echo "Started container $NAME from $IMAGE"
    echo "Box API: http://127.0.0.1:9000   script service: http://127.0.0.1:${PYTHON_SERVICE_HOST_PORT}"
}

cmd_down() {
    docker rm -f "$NAME" >/dev/null 2>&1 && echo "Removed container $NAME" || true
    if pid="$(agent_pid)"; then
        kill "$pid"
        for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        echo "Stopped host agent (pid $pid)"
    fi
    rm -f "$PID_FILE"
}

cmd_status() {
    if pid="$(agent_pid)"; then echo "host agent: running (pid $pid)"; else echo "host agent: stopped"; fi
    if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
        echo "container:  running ($NAME)"
        echo "link:       $(docker exec "$NAME" python3 -c \
            'import urllib.request; print(urllib.request.urlopen("http://localhost:8080/_hostlink/status", timeout=10).read().decode())' \
            2>&1)"
    else
        echo "container:  stopped"
    fi
}

cmd_logs() {
    echo "== host agent ($LOG_DIR/agent.log)"
    tail -n 40 "$LOG_DIR/agent.log" 2>/dev/null || true
    echo "== relay (container:/tmp/lager-hardware-service.log)"
    docker exec "$NAME" tail -n 40 /tmp/lager-hardware-service.log 2>/dev/null || true
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    logs) cmd_logs ;;
    *) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
