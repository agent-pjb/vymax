"""Colour palettes for the GUI. Pure data - no Tk imports, so it is easy to test."""

from __future__ import annotations

PALETTES = {
    "dark": {
        "bg": "#1e1f22",            # window background
        "surface": "#2b2d31",       # inputs, file list, log, progress trough
        "button": "#383a40",
        "hover": "#44474d",
        "pressed": "#2b2d31",
        "fg": "#e3e5e8",
        "muted": "#9aa0a6",         # hints and secondary text
        "border": "#454850",
        "disabled": "#6d7178",
        "accent": "#3b82f6",        # primary button, progress bars, selected check boxes
        "accent_hover": "#5b9bff",
        "accent_dim": "#2f5fb3",
        "select_bg": "#2f5fb3",
        "select_fg": "#ffffff",
        "good": "#3fb950",
        "warn": "#d29922",
        "bad": "#ff6b63",
    },
    "light": {
        "bg": "#f0f0f0",
        "surface": "#ffffff",
        "button": "#e1e1e1",
        "hover": "#d4d4d4",
        "pressed": "#c4c4c4",
        "fg": "#1f1f1f",
        "muted": "#6b6b6b",
        "border": "#bdbdbd",
        "disabled": "#9a9a9a",
        "accent": "#2563eb",
        "accent_hover": "#3b78f0",
        "accent_dim": "#1d4ed8",
        "select_bg": "#cfe0ff",
        "select_fg": "#1f1f1f",
        "good": "#167030",
        "warn": "#755a00",
        "bad": "#c62828",
    },
}

DEFAULT_THEME = "dark"
