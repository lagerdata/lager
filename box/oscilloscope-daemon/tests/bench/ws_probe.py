# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Probe whether simple_websocket.Server can upgrade under this WSGI server.

Diagnostic for the :9000 relay. Run inside the box container; it starts a
throwaway Flask app in the same configuration box_http_server uses and reports
exactly why an upgrade fails, which the 400 the relay returns cannot express.
"""
import logging
import sys
import threading
import time

logging.basicConfig(level=logging.INFO)

from flask import Flask, request
from flask_socketio import SocketIO
import simple_websocket

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

RESULT = {}


@app.before_request
def _trace_request():
    RESULT["before_request_ran"] = True
    RESULT["before_path"] = request.path


@app.errorhandler(Exception)
def _trace_error(e):
    import traceback
    RESULT["handler_error"] = "%s: %s" % (type(e).__name__, e)
    RESULT["handler_traceback"] = traceback.format_exc()
    raise e


@app.route("/probe", websocket=True)
def probe():
    keys = sorted(k for k in request.environ if "socket" in k.lower() or k.startswith("HTTP_"))
    RESULT["environ_keys"] = keys
    RESULT["has_werkzeug_socket"] = "werkzeug.socket" in request.environ
    try:
        ws = simple_websocket.Server(request.environ)
    except BaseException as e:
        RESULT["error"] = "%s: %s" % (type(e).__name__, e)
        import traceback
        RESULT["traceback"] = traceback.format_exc()
        return "", 400
    RESULT["error"] = None
    try:
        msg = ws.receive(timeout=5)
        ws.send("echo:" + str(msg))
        ws.close()
    except BaseException as e:
        RESULT["post_error"] = "%s: %s" % (type(e).__name__, e)
    return ""


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9111
    t = threading.Thread(
        target=lambda: socketio.run(app, host="127.0.0.1", port=port,
                                    allow_unsafe_werkzeug=True),
        daemon=True)
    t.start()
    time.sleep(2)

    try:
        client = simple_websocket.Client("ws://127.0.0.1:%d/probe" % port)
        client.send("hello")
        print("UPGRADE: OK, echo =", client.receive(timeout=5))
        client.close()
    except BaseException as e:
        print("UPGRADE: FAILED %s: %s" % (type(e).__name__, e))

    time.sleep(0.5)
    print("before_request ran:", RESULT.get("before_request_ran"), RESULT.get("before_path"))
    print("flask error handler:", RESULT.get("handler_error"))
    if RESULT.get("handler_traceback"):
        print(RESULT["handler_traceback"])
    print("has werkzeug.socket:", RESULT.get("has_werkzeug_socket"))
    print("server-side error:", RESULT.get("error"))
    if RESULT.get("traceback"):
        print(RESULT["traceback"])
    print("post-upgrade error:", RESULT.get("post_error"))
    hdrs = [k for k in RESULT.get("environ_keys", []) if "UPGRADE" in k or "CONNECTION" in k or "WEBSOCKET" in k]
    print("upgrade headers seen:", hdrs)


if __name__ == "__main__":
    main()
