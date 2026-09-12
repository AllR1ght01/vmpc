"""Reading the system clipboard, so Ctrl+V pastes.

prompt_toolkit binds ``c-v`` *and* ``s-insert`` to a do-nothing handler in its
basic bindings — deliberately, since in vi mode Ctrl+V is quoted-insert. In a
terminal that hands those keys to the application rather than pasting itself
(legacy conhost, and Windows Terminal once the paste shortcut is remapped) the
result is that a paste vanishes with no feedback at all. That is unpleasant
anywhere and actively confusing in the api-key field, where the text is masked
and "nothing appeared" and "it pasted invisibly" look identical.

So vmpc reads the clipboard itself. No new dependency: ctypes talks to the
Windows clipboard directly, and the other platforms have a command for it.
Every failure path returns an empty string — a paste that cannot happen is
worth nothing on screen, and never worth an exception out of a keypress.
"""

from __future__ import annotations

import os
import subprocess
import sys

#: Belt and braces against a clipboard holding a whole file. A key is a hundred
#: characters and a pasted prompt is a few thousand; a megabyte of it is an
#: accident, and inserting it into a single-line field would hang the render.
MAX_PASTE = 100_000

#: Commands to try elsewhere, in order. Wayland first on Linux because a session
#: with both usually has the X11 one talking to the wrong clipboard.
_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("wl-paste", "--no-newline"),
    ("xclip", "-selection", "clipboard", "-o"),
    ("xsel", "--clipboard", "--output"),
)


def read_clipboard() -> str:
    """The clipboard's text, or ``""`` if there is none to be had."""
    try:
        if os.name == "nt":
            return _windows()[:MAX_PASTE]
        if sys.platform == "darwin":
            return _command(("pbpaste",))[:MAX_PASTE]
        for command in _COMMANDS:
            text = _command(command)
            if text:
                return text[:MAX_PASTE]
        return ""
    except Exception:  # noqa: BLE001 - a keypress must not raise
        return ""


def paste_text(text: str, single_line: bool) -> str:
    """Normalize clipboard text for insertion into a buffer.

    ``single_line`` collapses the paste to its first non-empty line. A key copied
    out of a web page or a password manager carries a trailing newline, and in a
    one-line field a raw ``\\r`` is at best meaningless and at worst submits the
    form with half the value in it.
    """
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    if not single_line:
        return cleaned
    for line in cleaned.split("\n"):
        if line.strip():
            return line.strip()
    return ""


# --------------------------------------------------------------------------
# Platforms
# --------------------------------------------------------------------------


def _command(argv: tuple[str, ...]) -> str:
    try:
        finished = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(argv),
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if finished.returncode != 0:
        return ""
    return finished.stdout.decode("utf-8", errors="replace")


#: CF_UNICODETEXT. The ANSI format would mangle a non-Latin paste, and there is
#: no reason to ask for it when every Windows we support has the wide one.
_CF_UNICODETEXT = 13


def _windows() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    # Another process can hold the clipboard open for a moment after a copy, and
    # a single OpenClipboard would then fail for no lasting reason.
    for _attempt in range(5):
        if user32.OpenClipboard(None):
            break
        import time

        time.sleep(0.01)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.c_wchar_p(pointer).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        # Always: a clipboard left open blocks every other program's copy.
        user32.CloseClipboard()


# --------------------------------------------------------------------------
# Bindings
# --------------------------------------------------------------------------


def paste_bindings(single_line: bool = False, into=None):  # noqa: ANN001, ANN201
    """Key bindings that make Ctrl+V and Shift+Insert paste.

    Ours win over prompt_toolkit's ``_ignore`` handler because a session's own
    bindings are merged in front of the basic ones. ``into`` extends an existing
    :class:`~prompt_toolkit.key_binding.KeyBindings` instead of returning a new
    one, so a widget that already has bindings keeps them.

    Terminals that paste on their own — Windows Terminal by default, most Unix
    ones via bracketed paste — never deliver these keys, so adding them costs
    those users nothing.
    """
    from prompt_toolkit.key_binding import KeyBindings

    bindings = into if into is not None else KeyBindings()

    @bindings.add("c-v")
    @bindings.add("s-insert")
    def _paste(event) -> None:  # noqa: ANN001
        text = paste_text(read_clipboard(), single_line)
        if text:
            event.current_buffer.insert_text(text)

    return bindings


__all__ = ["MAX_PASTE", "paste_bindings", "paste_text", "read_clipboard"]
