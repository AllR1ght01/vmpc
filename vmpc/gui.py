"""Public desktop entry point for the HTML application."""

from __future__ import annotations

from vmpc.config import Config
from vmpc.webapp import main, run_html_gui


def run_gui(config: Config) -> int:
    return run_html_gui(config)


__all__ = ["run_gui", "main"]
