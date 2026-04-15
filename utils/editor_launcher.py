"""
Open files in the user's preferred local editor.

Preference-driven launch chain
------------------------------
The user picks their preferred editor in Settings. The choice is
stored as a short code in ``utils.settings["preferred_editor"]``
and an optional executable path in
``utils.settings["custom_editor_path"]``:

    ``auto``      — Notepad++ → Notepad → system default. The
                    application default. Text files go through the
                    editor chain; binary files go straight to the
                    system default opener.
    ``notepadpp`` — Notepad++, with Notepad + system default as
                    fallbacks.
    ``notepad``   — Notepad, with system default as fallback.
    ``vscode``    — VS Code, with Notepad++ / Notepad / system
                    default as fallbacks.
    ``system``    — system default only (``os.startfile`` /
                    ``open`` / ``xdg-open``).
    ``custom``    — a user-supplied executable path, with system
                    default as fallback if the path is missing.

Every launch is fire-and-forget via ``subprocess.Popen`` so a slow-
starting editor never blocks the Qt event loop. Detection results
are cached per-process; ``clear_detection_cache()`` refreshes them
(used by the Settings dialog's "Refresh detection" button).

Design notes
------------
* Local file opening is **always direct** — the caller passes the
  live path on disk and the editor launcher spawns the editor with
  that path. No temp copies, no staging. Remote-file opening is a
  separate concern handled by the File Transfer view, which
  downloads to a session temp cache and then calls open_file() on
  the staged path.
* ``.cmd`` / ``.bat`` launchers (e.g. ``code.cmd``) are wrapped in
  ``cmd /c`` on Windows because ``CreateProcess`` can't execute
  them directly.
* ``open_file`` raises ``EditorError`` only on a clean *pre-launch*
  failure (bad path, every fallback unavailable). Anything after
  the subprocess starts is out of our hands.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Callable, Optional


# ── Preference codes ──────────────────────────────────────────────────────

PREF_AUTO      = "auto"
PREF_NOTEPADPP = "notepadpp"
PREF_NOTEPAD   = "notepad"
PREF_VSCODE    = "vscode"
PREF_SYSTEM    = "system"
PREF_CUSTOM    = "custom"

PREF_ORDER: list[str] = [
    PREF_AUTO, PREF_NOTEPADPP, PREF_NOTEPAD, PREF_VSCODE, PREF_SYSTEM, PREF_CUSTOM,
]

PREF_LABELS: dict[str, str] = {
    PREF_AUTO:      "Auto — Notepad++ → Notepad → system default",
    PREF_NOTEPADPP: "Notepad++",
    PREF_NOTEPAD:   "Notepad",
    PREF_VSCODE:    "VS Code",
    PREF_SYSTEM:    "System default",
    PREF_CUSTOM:    "Custom executable…",
}


# ── File-type detection ───────────────────────────────────────────────────

_TEXT_EXTS: frozenset[str] = frozenset({
    # plain text / logs
    ".txt", ".log", ".out", ".err",
    # config formats
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".properties", ".env", ".editorconfig",
    # markup / docs
    ".md", ".markdown", ".rst", ".adoc", ".tex",
    ".html", ".htm", ".xml", ".svg", ".xhtml",
    # scripts / programming
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".py", ".pyw", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rb", ".php", ".lua", ".pl", ".pm", ".r",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh",
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".go", ".rs", ".swift", ".dart", ".vala",
    ".cs", ".vb", ".fs",
    # data
    ".csv", ".tsv", ".sql", ".gql", ".graphql",
    # styling
    ".css", ".scss", ".sass", ".less", ".styl",
    # patches / diffs
    ".diff", ".patch",
    # infra
    ".dockerfile", ".dockerignore", ".gitignore", ".gitattributes",
    ".rc", ".service", ".desktop", ".unit",
})

# Special filenames (no extension or unconventional) that are still
# always text. Compared case-insensitively.
_TEXT_NAMES_LOWER: frozenset[str] = frozenset({
    "dockerfile", "makefile", "cmakelists.txt", "jenkinsfile",
    "readme", "license", "changelog", "authors", "contributing",
    "vagrantfile", "rakefile", "gemfile", "procfile",
    ".bashrc", ".zshrc", ".profile", ".vimrc", ".tmux.conf",
    ".gitignore", ".dockerignore", ".editorconfig",
    "hosts", "resolv.conf", "fstab", "crontab",
})


def is_text_file(path: str) -> bool:
    """Return True if ``path`` looks like a text / code / config file."""
    name = os.path.basename(path)
    lower = name.lower()
    if lower in _TEXT_NAMES_LOWER:
        return True
    _root, ext = os.path.splitext(lower)
    if ext and ext in _TEXT_EXTS:
        return True
    return False


# ── Editor detection (cached) ─────────────────────────────────────────────

_notepadpp_cache: Optional[str] = None
_notepad_cache: Optional[str] = None
_vscode_cache: Optional[str] = None


def clear_detection_cache() -> None:
    """
    Drop cached editor paths so the next lookup re-scans PATH and
    every known install location. Called by the Settings dialog's
    "Refresh detection" button so a freshly-installed Notepad++
    shows up without restarting the app.
    """
    global _notepadpp_cache, _notepad_cache, _vscode_cache
    _notepadpp_cache = None
    _notepad_cache = None
    _vscode_cache = None


def find_notepadpp() -> Optional[str]:
    """Return a path to Notepad++ or None if it's not installed."""
    global _notepadpp_cache
    if _notepadpp_cache is not None:
        return _notepadpp_cache or None

    for name in ("notepad++", "notepad++.exe"):
        p = shutil.which(name)
        if p:
            _notepadpp_cache = p
            return p

    if sys.platform.startswith("win"):
        candidates: list[str] = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Notepad++", "notepad++.exe"))
        for c in candidates:
            if os.path.isfile(c):
                _notepadpp_cache = c
                return c

    _notepadpp_cache = ""
    return None


def find_notepad() -> Optional[str]:
    """
    Return a path to Notepad (Windows) or a text fallback on other OSes.

    Non-Windows platforms don't ship Notepad; the chain collapses to
    ``None`` there and the caller moves on to the OS default opener.
    """
    global _notepad_cache
    if _notepad_cache is not None:
        return _notepad_cache or None

    if sys.platform.startswith("win"):
        for name in ("notepad.exe", "notepad"):
            p = shutil.which(name)
            if p:
                _notepad_cache = p
                return p
        windir = os.environ.get("windir") or r"C:\Windows"
        guess = os.path.join(windir, "notepad.exe")
        if os.path.isfile(guess):
            _notepad_cache = guess
            return guess
        _notepad_cache = ""
        return None

    # Non-Windows: explicitly do NOT fall back to gedit/kate/nano/vi.
    # The preference model asked for "Notepad" specifically; if the
    # user is on Linux and picks "Notepad" they really mean "no
    # editor preference, just use OS default".
    _notepad_cache = ""
    return None


def find_vscode() -> Optional[str]:
    """Return a path to a working VS Code launcher, or None."""
    global _vscode_cache
    if _vscode_cache is not None:
        return _vscode_cache or None

    # 1. PATH lookup — covers the case where the user ran
    #    VS Code's "Shell Command: Install 'code' command in PATH".
    for name in ("code", "code-insiders", "code.cmd", "code.exe"):
        p = shutil.which(name)
        if p:
            _vscode_cache = p
            return p

    # 2. Windows standard install locations (user + system).
    if sys.platform.startswith("win"):
        candidates: list[str] = []
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates += [
                os.path.join(local, "Programs", "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe"),
                os.path.join(local, "Programs", "Microsoft VS Code Insiders", "bin", "code-insiders.cmd"),
            ]
        pf = os.environ.get("ProgramFiles", "")
        if pf:
            candidates += [
                os.path.join(pf, "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(pf, "Microsoft VS Code", "Code.exe"),
            ]
        pfx86 = os.environ.get("ProgramFiles(x86)", "")
        if pfx86:
            candidates += [
                os.path.join(pfx86, "Microsoft VS Code", "bin", "code.cmd"),
                os.path.join(pfx86, "Microsoft VS Code", "Code.exe"),
            ]
        for c in candidates:
            if c and os.path.isfile(c):
                _vscode_cache = c
                return c

    # 3. macOS standard install location.
    if sys.platform == "darwin":
        mac_paths = [
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            os.path.expanduser(
                "~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"
            ),
        ]
        for c in mac_paths:
            if os.path.isfile(c):
                _vscode_cache = c
                return c

    _vscode_cache = ""
    return None


# ── Preference helpers ────────────────────────────────────────────────────

def get_editor_preference() -> tuple[str, str]:
    """Return ``(preference_code, custom_path)`` from persisted settings."""
    try:
        from utils import settings
    except Exception:
        return PREF_AUTO, ""
    pref = settings.get("preferred_editor", PREF_AUTO) or PREF_AUTO
    if pref not in PREF_LABELS:
        pref = PREF_AUTO
    custom = settings.get("custom_editor_path", "") or ""
    return pref, custom


def set_editor_preference(pref: str, custom_path: Optional[str] = None) -> None:
    """
    Persist the preferred-editor choice. ``pref`` must be one of the
    ``PREF_*`` codes; unknown codes silently become ``PREF_AUTO``.
    """
    from utils import settings
    if pref not in PREF_LABELS:
        pref = PREF_AUTO
    settings.set_value("preferred_editor", pref)
    if custom_path is not None:
        settings.set_value("custom_editor_path", custom_path)


def describe_current_preference() -> str:
    """Short human label for the active preference, for UI status readouts."""
    pref, custom = get_editor_preference()
    if pref == PREF_CUSTOM:
        if custom:
            return f"Custom ({os.path.basename(custom)})"
        return "Custom (not set)"
    return PREF_LABELS.get(pref, pref)


# ── Launching ─────────────────────────────────────────────────────────────

class EditorError(Exception):
    """Raised when every launch path for a file has failed."""


_LauncherFn = Callable[[str], None]


def open_file(
    path: str,
    *,
    preference: Optional[str] = None,
    custom_path: Optional[str] = None,
    prefer_text_editor: bool = True,
) -> str:
    """
    Open ``path`` in the user's preferred local editor.

    Parameters
    ----------
    path
        Absolute path on disk. Directories are rejected — the caller
        (the file-transfer view) handles folder navigation.
    preference
        One of the ``PREF_*`` codes. Defaults to whatever is saved
        in ``utils.settings``. Explicit values are only useful for
        tests — production callers omit this.
    custom_path
        Path to a custom editor executable. Used only when
        ``preference`` resolves to ``PREF_CUSTOM``.
    prefer_text_editor
        When False, skip the text-editor chain entirely and go
        straight to the system default opener. Callers set this
        for binary-only flows.

    Returns
    -------
    str
        Short human-readable description of what was launched,
        for UI status output (``"Notepad++"``, ``"System default"``,
        etc).

    Raises
    ------
    EditorError
        The path doesn't exist, isn't a regular file, or every
        launch attempt on the preference chain failed before the
        editor process even started.
    """
    if not path:
        raise EditorError("Empty path")
    if not os.path.exists(path):
        raise EditorError(f"Not found: {path}")
    if not os.path.isfile(path):
        raise EditorError(f"Not a regular file: {path}")

    if preference is None:
        pref_code, saved_custom = get_editor_preference()
        if custom_path is None:
            custom_path = saved_custom
    else:
        pref_code = preference
        if pref_code not in PREF_LABELS:
            pref_code = PREF_AUTO
        if custom_path is None:
            custom_path = ""

    use_text_chain = prefer_text_editor and is_text_file(path)
    chain = _build_chain(pref_code, custom_path or "", use_text_chain)

    tried: list[str] = []
    for label, launcher in chain:
        try:
            launcher(path)
            return label
        except FileNotFoundError as exc:
            tried.append(f"{label} ({exc})")
            continue
        except PermissionError as exc:
            tried.append(f"{label} (permission denied: {exc})")
            continue
        except Exception as exc:
            tried.append(f"{label} ({exc})")
            continue

    raise EditorError(
        "Could not launch any editor for "
        + os.path.basename(path)
        + " — tried: "
        + "; ".join(tried or ["(nothing)"])
    )


# ── Chain builder ─────────────────────────────────────────────────────────

def _build_chain(
    preference: str,
    custom_path: str,
    use_text_chain: bool,
) -> list[tuple[str, _LauncherFn]]:
    """
    Construct the ordered launch chain for the given preference.

    Every preference ends with the system default opener as a final
    fallback so a missing editor still opens the file *somewhere*
    instead of raising.
    """
    chain: list[tuple[str, _LauncherFn]] = []

    def _mk_editor(label: str, finder: Callable[[], Optional[str]]) -> _LauncherFn:
        def _do(p: str) -> None:
            cmd = finder()
            if not cmd:
                raise FileNotFoundError(f"{label} is not installed")
            _spawn_with(cmd, p)
        return _do

    def _mk_custom() -> _LauncherFn:
        def _do(p: str) -> None:
            if not custom_path:
                raise FileNotFoundError("Custom editor path is not configured")
            if not os.path.isfile(custom_path):
                raise FileNotFoundError(
                    f"Custom editor not found: {custom_path}"
                )
            _spawn_with(custom_path, p)
        return _do

    def _os_default_launcher() -> _LauncherFn:
        def _do(p: str) -> None:
            _os_default_open(p)
        return _do

    def _append_editor(label: str, finder: Callable[[], Optional[str]]) -> None:
        chain.append((label, _mk_editor(label, finder)))

    def _append_custom() -> None:
        label = (
            "Custom (" + os.path.basename(custom_path) + ")"
            if custom_path else "Custom editor"
        )
        chain.append((label, _mk_custom()))

    def _append_os_default() -> None:
        chain.append(("System default", _os_default_launcher()))

    if preference == PREF_CUSTOM:
        _append_custom()
        _append_os_default()
        return chain

    if preference == PREF_SYSTEM:
        _append_os_default()
        return chain

    if preference == PREF_NOTEPADPP:
        _append_editor("Notepad++", find_notepadpp)
        _append_editor("Notepad", find_notepad)
        _append_os_default()
        return chain

    if preference == PREF_NOTEPAD:
        _append_editor("Notepad", find_notepad)
        _append_os_default()
        return chain

    if preference == PREF_VSCODE:
        _append_editor("VS Code", find_vscode)
        _append_editor("Notepad++", find_notepadpp)
        _append_editor("Notepad", find_notepad)
        _append_os_default()
        return chain

    # preference == PREF_AUTO (application default)
    #
    # Text files walk the default chain; binary files skip straight
    # to the OS default opener so the user's image viewer / PDF
    # reader / etc. handle them naturally.
    if use_text_chain:
        _append_editor("Notepad++", find_notepadpp)
        _append_editor("Notepad", find_notepad)
    _append_os_default()
    return chain


# ── Spawning primitives ──────────────────────────────────────────────────

def _creation_flags() -> int:
    """
    Return ``subprocess`` creation flags that keep the Qt process
    insulated from the child — on Windows that means no flashing
    console window for ``.cmd`` launchers.
    """
    if sys.platform.startswith("win"):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _spawn_with(command: str, file_path: str) -> None:
    """
    Launch ``command file_path`` fire-and-forget.

    Handles the Windows ``.cmd`` / ``.bat`` quirk: ``CreateProcess``
    cannot run them directly, so we wrap through ``cmd /c``.
    """
    cmd_lower = command.lower()
    is_windows = sys.platform.startswith("win")
    if is_windows and cmd_lower.endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", command, file_path]
    else:
        argv = [command, file_path]

    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        creationflags=_creation_flags(),
    )


def _os_default_open(path: str) -> None:
    """Delegate to the platform's "open with default app" facility."""
    if sys.platform.startswith("win"):
        # os.startfile is the right API on Windows — it triggers the
        # same "Open" verb that Explorer uses.
        os.startfile(path)  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(
            ["open", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.Popen(
        ["xdg-open", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
