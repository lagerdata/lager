# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Color themes and styling for the Lager Terminal
"""
from prompt_toolkit.styles import Style

# Primary colors
PRIMARY = "#00aa00"      # Green (for logo and success)
ERROR = "#ff5555"        # Red
WARNING = "#ffaa00"      # Orange
DIM = "#666666"          # Gray for secondary text
MUTED = "#888888"        # Lighter gray
INFO = "#5555ff"         # Blue for info messages

# Rich console colors (for rich library)
RICH_COLORS = {
    "primary": "green",
    "error": "red",
    "warning": "yellow",
    "dim": "dim",
    "muted": "bright_black",
    "info": "blue",
    "success": "green",
}

# Prompt styling for prompt_toolkit
PROMPT_STYLE = Style.from_dict({
    'prompt': f'{PRIMARY} bold',
    '': '#ffffff',
    'completion-menu.completion': 'bg:#333333 #ffffff',
    'completion-menu.completion.current': 'bg:#00aa00 #000000',
    'auto-suggest': '#666666',
})

# Output status indicators
SUCCESS_INDICATOR = "[green]✓[/green]"
ERROR_INDICATOR = "[red]✗[/red]"
WARNING_INDICATOR = "[yellow]![/yellow]"
INFO_INDICATOR = "[blue]i[/blue]"
