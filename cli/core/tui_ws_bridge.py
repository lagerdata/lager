# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Deliver WebSocket client callbacks to a Textual app safely."""

import threading

_CALLBACKS = ("on_state_update", "on_error", "on_connected", "on_disconnected")


class WebSocketBridgeMixin:
    """Mix into a textual App that owns a WebSocket client in ``self.ws_client``."""

    ws_client = None
    _ws_closing = False
    _ws_app_thread = None

    def _bind_ws_callbacks(self, **handlers) -> None:
        """Register handlers on ``self.ws_client``; call from the app thread."""
        self._ws_app_thread = threading.get_ident()
        for name, handler in handlers.items():
            setattr(self.ws_client, name, self._marshal(handler))

    def _marshal(self, handler):
        def run(*args):
            if not self._ws_closing:
                handler(*args)

        def deliver(*args):
            if self._ws_closing:
                return
            if threading.get_ident() == self._ws_app_thread:
                run(*args)
                return
            # Textual is not thread-safe and Socket.IO calls back on its own
            # threads, so widget work has to be handed to the app's thread.
            try:
                self.call_from_thread(run, *args)
            except RuntimeError:
                pass  # The app has stopped; nothing is left to update.

        return deliver

    def _close_ws(self) -> None:
        """Stop delivering events, then tell the box to stop and disconnect."""
        if self._ws_closing:
            return
        self._ws_closing = True
        client = getattr(self, "ws_client", None)
        if client is None:
            return
        for name in _CALLBACKS:
            setattr(client, name, None)
        try:
            client.disconnect()
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._close_ws()
