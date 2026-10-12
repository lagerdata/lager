# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Key handling shared by the battery and supply TUIs."""

from textual.binding import Binding

# Priority, so the focused Input cannot take the key: older Textual releases
# (3.2.0) run its copy action on ctrl+c even with nothing selected, so the TUI
# never quit.
COPY_OR_QUIT_BINDING = Binding("ctrl+c", "copy_or_quit", "Quit", priority=True)


class CopyOrQuitMixin:
    """Ctrl+C copies the current selection if there is one, else quits."""

    def action_copy_or_quit(self) -> None:
        focused = self.focused
        selected = getattr(focused, "selected_text", "") if focused is not None else ""
        if not selected:
            selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
        else:
            self.action_quit()
