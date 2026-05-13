"""
Help / FAQ page — practical, structured in-app documentation.

Layout::

    +-----------+-----------------------------------------------+
    |           |                                               |
    |  SIDE     |   <content for the selected section>          |
    |  INDEX    |                                               |
    |           |                                               |
    +-----------+-----------------------------------------------+

The left rail is a mini-TOC; the right pane shows a scrollable,
theme-aware rich-text view rendered from Markdown-ish content.
Content is defined as inline Python data so it travels with the app
and can never diverge from the running code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QButtonGroup, QStackedWidget, QTextBrowser, QScrollArea, QSizePolicy,
)

from gui.themes import theme, ThemeManager


# ── Content ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HelpSection:
    key: str
    title: str
    body_html: str     # pre-rendered HTML (relies on theme for colour)


def _section(key: str, title: str, *blocks: str) -> HelpSection:
    return HelpSection(key=key, title=title, body_html="\n".join(blocks))


def _h(text: str) -> str:
    return f"<h2>{text}</h2>"


def _p(text: str) -> str:
    return f"<p>{text}</p>"


def _steps(*items: str) -> str:
    rows = "".join(f"<li>{s}</li>" for s in items)
    return f"<ol>{rows}</ol>"


def _bullets(*items: str) -> str:
    rows = "".join(f"<li>{s}</li>" for s in items)
    return f"<ul>{rows}</ul>"


def _kbd(text: str) -> str:
    return f"<span class='kbd'>{text}</span>"


def _faq(question: str, answer: str) -> str:
    return (
        f"<div class='faq'><div class='q'>{question}</div>"
        f"<div class='a'>{answer}</div></div>"
    )


HELP_SECTIONS: List[HelpSection] = [
    _section(
        "quickstart", "Quick Start",
        _p("Net Engine is a local-first network workstation. Pick a "
           "page from the sidebar on the left — each page is a "
           "self-contained tool."),
        _h("First-run walkthrough"),
        _steps(
            "Open <b>Scanner</b>. Pick an interface, choose a preset "
            f"(or type a CIDR like <code>192.168.1.0/24</code>), "
            "press <b>Scan</b>.",
            "Select any live host in the results to see its details, "
            "open ports, and a quick-action drawer on the right.",
            "Use the drawer to <b>Ping</b>, <b>Port Scan</b>, or "
            "<b>SSH</b> straight into that host.",
            "Switch to <b>File Transfer</b> for SFTP, <b>Terminal</b> "
            "for a local shell, or <b>Assistant</b> for local AI help.",
        ),
        _h("Keyboard shortcuts"),
        _bullets(
            f"{_kbd('Ctrl+1')} … {_kbd('Ctrl+9')} — jump to a page",
            f"{_kbd('Ctrl+B')} — collapse / expand the sidebar",
            f"{_kbd('Ctrl+,')} — open Settings",
            f"{_kbd('F5')} — start a scan",
            f"{_kbd('Esc')} — stop a running scan",
            f"{_kbd('Ctrl+E')} — export scan results",
        ),
    ),
    _section(
        "scanner", "Scanner",
        _p("Find live hosts on a subnet, then enrich each one with "
           "hostname, MAC, vendor, latency, and open-port hints."),
        _h("How to scan"),
        _steps(
            "Pick your network interface from the left dropdown.",
            "Pick a scan preset (Quick / Extended / Full) or type "
            "a CIDR range directly.",
            "Click <b>Scan</b> or press F5. Progress shows in the "
            "status bar at the bottom.",
            "Click any row to pin the host for details; right-click "
            "for more actions.",
        ),
        _h("Detail drawer"),
        _p("Select a host and the right-hand drawer shows extended "
           "info plus quick actions — Ping, Port Scan, Open SSH, "
           "Quick Connect, and Copy IP."),
        _h("Exporting"),
        _p("Use <b>File → Export Results…</b> or press "
           f"{_kbd('Ctrl+E')} to save results as CSV or JSON."),
    ),
    _section(
        "ssh", "SSH Sessions",
        _p("A full-screen, multi-session SSH workspace. Each connection "
           "becomes its own tab with a fresh terminal, live state, and "
           "persistent scrollback."),
        _h("Opening a session"),
        _steps(
            "Go to <b>SSH Sessions</b> from the sidebar.",
            "Fill in <b>host</b>, <b>port</b>, <b>user</b>, and either "
            "a password or the path to a private key file.",
            "Click <b>Connect</b>. A new tab opens once the handshake "
            "finishes; status shows in the bottom status bar.",
            "Use the <b>Save</b> button to remember a host shortcut "
            "for next time — saved hosts appear in the dropdown.",
        ),
        _h("From the Scanner"),
        _p("From the Scanner detail drawer you can press <b>SSH</b> "
           "to jump into the SSH page with the target IP pre-filled, "
           "or <b>Quick Connect</b> to connect immediately using a "
           "saved profile."),
        _h("Terminal focus mode"),
        _p("Press <b>F11</b> (or the focus button) to hide the sidebar, "
           "status bar, and menu so the terminal claims the whole "
           "window. Press it again to restore."),
    ),
    _section(
        "terminal", "Terminal",
        _p("A local shell embedded in the app. Great for scripting, "
           "poking interfaces, or keeping a REPL alongside your scans."),
        _h("Switching shells"),
        _p("Use the shell dropdown in the terminal header. Available "
           "options depend on your OS — PowerShell, CMD, and WSL on "
           "Windows; Bash on Linux/macOS. Only installed shells are "
           "offered."),
        _h("AI → Terminal handoff"),
        _p("In the <b>Assistant</b> page, click <b>Insert into "
           "Terminal</b> on any suggested command. Net Engine "
           "switches to this page and pre-fills your input. "
           "<b>Commands are never auto-run</b> — review and press "
           "Enter yourself."),
    ),
    _section(
        "filetransfer", "File Transfer",
        _p("Dual-pane SFTP browser. The left pane is your local "
           "machine; the right pane is any SSH session you have open "
           "on the <b>SSH Sessions</b> page."),
        _h("Moving files"),
        _steps(
            "Open an SSH session first on the SSH Sessions page.",
            "Come back to <b>File Transfer</b>. Pick the session from "
            "the remote-pane dropdown.",
            "Drag files between panes, or use the toolbar buttons.",
            "Active transfers show a progress strip; failures surface "
            "with a clear reason.",
        ),
        _h("Open vs Download"),
        _bullets(
            "<b>Open</b> downloads the remote file to a session "
            "temp folder and launches it in your preferred editor or "
            "the OS default app. Changes on disk remain local — the "
            "file is not auto-synced back.",
            "<b>Download</b> saves the file to a location you pick "
            "and then does nothing else.",
        ),
        _p("Pick your editor preference in <b>Settings → Editor</b>. "
           "The default <i>Auto</i> chain is Notepad++ → Notepad → "
           "the system default."),
    ),
    _section(
        "adapter", "Network Adapter",
        _p("Inspect and reconfigure your machine's network interfaces "
           "without dropping to the command line."),
        _bullets(
            "View IP, mask, gateway, DNS for every adapter.",
            "Switch an adapter between DHCP and a static IP.",
            "Save a set of settings as a named profile for fast "
            "switching between home/work/lab.",
        ),
        _p("<b>Heads up:</b> applying adapter changes usually needs "
           "administrator privileges. Launch Net Engine with the "
           "included <code>run-admin.bat</code> (Windows) or via sudo "
           "if a change is rejected."),
    ),
    _section(
        "monitor", "Monitor",
        _p("Live view of interface traffic, CPU, and memory. Use it "
           "next to Scanner or SSH when you want to confirm an "
           "interface is actually passing the bytes you expect."),
    ),
    _section(
        "tools", "Tools & API Console",
        _h("Tools"),
        _p("One-click diagnostic commands (ipconfig, arp, route, "
           "netsh / ip, etc.) plus a free-form command runner. "
           "Output is captured in a scrollback area; the activity "
           "log keeps a history of every run."),
        _h("API Console"),
        _bullets(
            "Built-in REST client: GET/POST/PUT/PATCH/DELETE.",
            "Custom headers, JSON/raw body, Basic + Bearer auth.",
            "Save named requests or import/export cURL strings.",
        ),
    ),
    _section(
        "assistant", "AI Assistant",
        _p("A local AI helper powered by <b>Ollama</b>. All inference "
           "runs on your machine — nothing ever leaves it."),
        _h("Setting it up"),
        _steps(
            "Install Ollama from ollama.com.",
            "Pull a small instruct model, for example: "
            "<code>ollama pull llama3.2:3b</code>.",
            "Open the <b>Assistant</b> page. If Ollama is reachable "
            "and the model is installed, the Send button goes live.",
            "Switch between <b>Command</b> (suggest a shell command) "
            "and <b>Chat</b> (free-form help) with the mode toggle.",
        ),
        _h("If it's not working"),
        _p("The Assistant shows an offline banner with the exact "
           "remedy. Common causes: Ollama isn't running, the base URL "
           "in Settings is wrong, or the model isn't pulled yet. The "
           "rest of the app keeps working normally either way."),
    ),
    _section(
        "settings", "Settings",
        _p("All persistent preferences live on the <b>Settings</b> "
           "page. Organised into five groups:"),
        _bullets(
            "<b>Appearance</b> — theme, theme-specific options "
            "(Liquid Glass opacity, OG Black accent).",
            "<b>AI</b> — enable/disable, Ollama base URL, timeout, "
            "max tokens, temperature.",
            "<b>Editor</b> — which editor opens files from the File "
            "Transfer page.",
            "<b>Terminal</b> — default shell used when the app starts.",
            "<b>General</b> — reveal the settings file, reset to "
            "defaults.",
        ),
        _p("Settings persist to "
           "<code>~/.netscope/settings.json</code>. Delete that file "
           "at any time to start from defaults."),
    ),
    _section(
        "faq", "FAQ",
        _faq(
            "How do I connect to a host over SSH?",
            "Go to <b>SSH Sessions</b>, fill in host / port / user, "
            "pick a password or key file, press <b>Connect</b>. "
            "Or: select a host in the Scanner and use the <b>SSH</b> "
            "button in the detail drawer.",
        ),
        _faq(
            "How do I switch terminal modes?",
            "Use the shell dropdown in the Terminal page's header. "
            "Only shells actually installed on your OS are listed.",
        ),
        _faq(
            "How do I browse remote files?",
            "Open an SSH session first, then go to <b>File Transfer</b> "
            "and select that session in the remote-pane dropdown.",
        ),
        _faq(
            "What is the difference between Open and Download?",
            "<b>Open</b> pulls the remote file to a temporary location "
            "and launches it in your preferred editor / default app. "
            "<b>Download</b> just saves it somewhere you pick.",
        ),
        _faq(
            "How do I change the editor used for files?",
            "Settings → <b>Editor</b>. Pick Notepad++, Notepad, VS "
            "Code, the system default, or a custom executable path.",
        ),
        _faq(
            "What happens if Ollama is not running?",
            "The Assistant page shows an offline banner explaining "
            "what to do. Nothing else in the app is affected.",
        ),
        _faq(
            "How do I change themes?",
            "Settings → <b>Appearance</b>, or use <b>View → Theme</b> "
            "from the menu bar.",
        ),
        _faq(
            "Where are my settings stored?",
            "<code>~/.netscope/settings.json</code>. Remove it to "
            "reset, or open its folder from Settings → General.",
        ),
        _faq(
            "Do I need administrator rights?",
            "Only for adapter reconfiguration on Windows. Everything "
            "else runs fine as a normal user.",
        ),
    ),
    _section(
        "troubleshoot", "Troubleshooting",
        _h("Scanner finds nothing"),
        _bullets(
            "Confirm the interface dropdown shows the adapter you "
            "actually use.",
            "Try a wider CIDR (e.g. <code>/24</code> instead of "
            "<code>/28</code>).",
            "Windows Defender Firewall can block ICMP; allow "
            "<i>File and Printer Sharing (Echo Request)</i> for the "
            "active profile.",
        ),
        _h("SSH: Permission denied"),
        _bullets(
            "Double-check user, key file path, and passphrase.",
            "On Windows, make sure the key file's permissions aren't "
            "world-readable — OpenSSH will reject it.",
            "Verify the server actually allows password login if "
            "you're not using a key.",
        ),
        _h("File Transfer: nothing in remote pane"),
        _p("The remote pane reuses live SSH sessions. If the dropdown "
           "is empty, open a session on <b>SSH Sessions</b> first."),
        _h("Assistant: status stays offline"),
        _bullets(
            "Run <code>ollama serve</code> and check the URL in "
            "Settings → AI matches (default "
            "<code>http://localhost:11434</code>).",
            "Pull the model you selected — the page tells you which "
            "tag is missing.",
            "Firewall rules on Windows sometimes block loopback for "
            "newly-installed services; allow <code>ollama.exe</code> "
            "for <i>Private networks</i>.",
        ),
    ),
]


# ── Widgets ────────────────────────────────────────────────────────────


class _IndexButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("help_index_btn")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)


class HelpView(QWidget):
    """Standalone Help / FAQ page rendered in the main workspace stack."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())
        # Default to the first section.
        if self._nav_buttons:
            self._nav_buttons[0].setChecked(True)
            self._on_index_clicked(0)

    # ── Build ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(14)

        # Header row.
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(12)

        self._lbl_title = QLabel("HELP & DOCUMENTATION")
        self._lbl_title.setObjectName("lbl_section")
        hdr_row.addWidget(self._lbl_title)
        hdr_row.addStretch(1)
        outer.addLayout(hdr_row)

        self._lbl_subtitle = QLabel(
            "Practical usage steps, common workflows, and answers to "
            "the questions most users hit first."
        )
        self._lbl_subtitle.setObjectName("help_subtitle")
        self._lbl_subtitle.setWordWrap(True)
        outer.addWidget(self._lbl_subtitle)

        body = QHBoxLayout()
        body.setSpacing(16)
        outer.addLayout(body, stretch=1)

        # ── Left rail: mini index ───────────────────────────────────
        rail = QFrame()
        rail.setObjectName("help_rail")
        rail.setFixedWidth(220)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(10, 14, 10, 14)
        rail_lay.setSpacing(4)

        self._lbl_index = QLabel("ON THIS PAGE")
        self._lbl_index.setObjectName("lbl_field_label")
        self._lbl_index.setContentsMargins(6, 0, 0, 6)
        rail_lay.addWidget(self._lbl_index)

        self._nav_buttons: list[_IndexButton] = []
        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)

        for i, sec in enumerate(HELP_SECTIONS):
            btn = _IndexButton(sec.title)
            btn.clicked.connect(lambda _c=False, idx=i: self._on_index_clicked(idx))
            self._nav_group.addButton(btn, i)
            rail_lay.addWidget(btn)
            self._nav_buttons.append(btn)

        rail_lay.addStretch(1)
        body.addWidget(rail, 0)

        # ── Right pane: content ────────────────────────────────────
        content_frame = QFrame()
        content_frame.setObjectName("help_content_frame")
        cf_lay = QVBoxLayout(content_frame)
        cf_lay.setContentsMargins(0, 0, 0, 0)
        cf_lay.setSpacing(0)

        self._stack = QStackedWidget()
        for sec in HELP_SECTIONS:
            tb = QTextBrowser()
            tb.setOpenExternalLinks(True)
            tb.setObjectName("help_body")
            tb.setFrameShape(QFrame.Shape.NoFrame)
            tb.setHtml(self._wrap(sec))
            self._stack.addWidget(tb)
        cf_lay.addWidget(self._stack)
        body.addWidget(content_frame, 1)

    # ── Handlers ─────────────────────────────────────────────────────

    @pyqtSlot(int)
    def _on_index_clicked(self, idx: int) -> None:
        if 0 <= idx < self._stack.count():
            self._stack.setCurrentIndex(idx)
            # Make sure the button is actually checked (handles
            # programmatic calls with no preceding click).
            if idx < len(self._nav_buttons):
                self._nav_buttons[idx].setChecked(True)
            tb = self._stack.currentWidget()
            if isinstance(tb, QTextBrowser):
                tb.verticalScrollBar().setValue(0)

    # ── Content wrapper ─────────────────────────────────────────────

    def _wrap(self, sec: HelpSection) -> str:
        """Wrap raw section body into the full themed HTML."""
        return (
            f"<h1>{sec.title}</h1>"
            f"{sec.body_html}"
        )

    # ── Theme ───────────────────────────────────────────────────────

    def _restyle(self, t) -> None:
        accent2 = t.accent2 or t.accent

        self.setStyleSheet(
            f"#help_rail {{"
            f"  background-color: {t.bg_raised};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 10px;"
            f"}}"
            f"#help_content_frame {{"
            f"  background-color: {t.bg_raised};"
            f"  border: 1px solid {t.border};"
            f"  border-radius: 10px;"
            f"}}"
            f"#help_subtitle {{"
            f"  color: {t.text_dim};"
            f"  font-size: 12px;"
            f"}}"
            f"QPushButton#help_index_btn {{"
            f"  background: transparent;"
            f"  color: {t.text_dim};"
            f"  border: none;"
            f"  border-left: 3px solid transparent;"
            f"  padding: 8px 12px;"
            f"  text-align: left;"
            f"  font-size: 12px;"
            f"  font-weight: 600;"
            f"  border-radius: 0;"
            f"  min-height: 22px;"
            f"}}"
            f"QPushButton#help_index_btn:hover {{"
            f"  background-color: {t.bg_hover};"
            f"  color: {t.text};"
            f"}}"
            f"QPushButton#help_index_btn:checked {{"
            f"  color: {t.accent};"
            f"  background-color: {t.bg_base};"
            f"  border-left: 3px solid {t.accent};"
            f"}}"
            f"QTextBrowser#help_body {{"
            f"  background-color: {t.bg_raised};"
            f"  color: {t.text};"
            f"  padding: 22px 26px 22px 26px;"
            f"  font-size: 13px;"
            f"}}"
        )

        # Rebuild the per-document stylesheet for Qt's rich text view —
        # QTextBrowser ignores most QSS, so we set a document stylesheet
        # and re-render the HTML.
        doc_css = (
            f"h1 {{"
            f"  color: {t.accent};"
            f"  font-size: 22px;"
            f"  font-weight: 800;"
            f"  letter-spacing: 1.4px;"
            f"  margin: 0 0 14px 0;"
            f"}}"
            f"h2 {{"
            f"  color: {accent2};"
            f"  font-size: 13px;"
            f"  font-weight: 800;"
            f"  letter-spacing: 1.6px;"
            f"  text-transform: uppercase;"
            f"  margin: 22px 0 8px 0;"
            f"}}"
            f"p {{"
            f"  color: {t.text};"
            f"  font-size: 13px;"
            f"  line-height: 160%;"
            f"  margin: 6px 0 10px 0;"
            f"}}"
            f"li {{"
            f"  color: {t.text};"
            f"  margin: 4px 0;"
            f"  line-height: 160%;"
            f"}}"
            f"b, strong {{ color: {t.text}; font-weight: 700; }}"
            f"i, em {{ color: {t.text}; }}"
            f"code {{"
            f"  color: {t.accent};"
            f"  background-color: {t.bg_base};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 12px;"
            f"  padding: 1px 6px;"
            f"  border-radius: 4px;"
            f"}}"
            f".kbd {{"
            f"  color: {t.accent};"
            f"  background-color: {t.bg_base};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 11px;"
            f"  font-weight: 700;"
            f"  padding: 1px 6px;"
            f"  border-radius: 4px;"
            f"}}"
            f".faq {{ margin: 10px 0 14px 0; }}"
            f".faq .q {{"
            f"  color: {t.accent};"
            f"  font-weight: 700;"
            f"  font-size: 13px;"
            f"  margin-bottom: 4px;"
            f"}}"
            f".faq .a {{"
            f"  color: {t.text};"
            f"  font-size: 13px;"
            f"  line-height: 160%;"
            f"}}"
        )

        for i in range(self._stack.count()):
            tb = self._stack.widget(i)
            if isinstance(tb, QTextBrowser):
                tb.document().setDefaultStyleSheet(doc_css)
                # Re-render with the new stylesheet.
                sec = HELP_SECTIONS[i]
                tb.setHtml(self._wrap(sec))

    def shutdown(self) -> None:
        pass
