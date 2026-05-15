"""
Settings page — the central place to configure Net Engine.

Five sections, each grouped under its own card:

  * **Appearance** — theme picker, theme-specific options (Liquid Glass
    opacity, OG Black accent colour).
  * **AI** — enable/disable, Ollama base URL, timeout, max tokens,
    temperature.
  * **Editor** — preferred external editor used when File Transfer
    opens a file (Auto / Notepad++ / Notepad / VS Code / System / Custom).
  * **Terminal** — default shell used on startup.
  * **File Transfer** — reset persistent panel / queue state.
  * **General** — settings file path, reveal folder, reset to defaults.

Every control is bound to a real persisted key in :mod:`utils.settings`
(or to :class:`ai.model_config.AIConfig` for the AI block). Changing a
value applies immediately where live-apply is safe (theme, opacity,
editor preference, terminal shell). Values that require a running
worker to be rebuilt (AI base URL, timeout, temperature, max tokens)
are applied with an explicit <b>Apply</b> button in the AI section so
in-flight requests are not silently restarted.
"""

from __future__ import annotations

import os
import platform
import subprocess
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QSlider, QGroupBox, QFormLayout, QCheckBox, QSpinBox,
    QDoubleSpinBox, QFileDialog, QMessageBox, QFrame, QScrollArea,
    QSizePolicy,
)

from gui.themes import theme, ThemeManager, OG_BLACK_ACCENTS
from utils import settings
from utils import editor_launcher as _ed
from gui.components.terminal_widget import (
    available_shell_names, default_shell_name,
)
from ai.model_config import load_config as load_ai_config, AIConfig


class SettingsView(QWidget):
    """Top-level Settings page rendered inside the main stack."""

    status_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ai_service_ref = None  # set via attach_ai_service
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Inter-page wiring ────────────────────────────────────────────

    def attach_ai_service(self, service) -> None:
        """Main window calls this after it has access to the live
        ``AIService`` (via the Assistant view). Storing the reference
        lets the AI block apply changes to the running service in
        addition to persisting them to disk."""
        self._ai_service_ref = service

    # ── Build ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Top header bar.
        header = QFrame()
        header.setObjectName("settings_header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(26, 20, 26, 16)
        hl.setSpacing(12)

        self._lbl_title = QLabel("SETTINGS")
        self._lbl_title.setObjectName("settings_title")
        hl.addWidget(self._lbl_title)
        hl.addStretch(1)

        self._lbl_path = QLabel(
            settings.settings_file_path()
        )
        self._lbl_path.setObjectName("settings_path")
        self._lbl_path.setToolTip("On-disk settings file")
        hl.addWidget(self._lbl_path, 0, Qt.AlignmentFlag.AlignVCenter)

        outer.addWidget(header)

        # Scroll area wraps the sections so the page remains usable at
        # small window heights.
        scroll = QScrollArea()
        scroll.setObjectName("settings_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        content = QWidget()
        content.setObjectName("settings_content")
        self._content = content
        body = QVBoxLayout(content)
        body.setContentsMargins(26, 10, 26, 24)
        body.setSpacing(16)

        body.addWidget(self._build_appearance_group())
        body.addWidget(self._build_ai_group())
        body.addWidget(self._build_editor_group())
        body.addWidget(self._build_terminal_group())
        body.addWidget(self._build_transfer_group())
        body.addWidget(self._build_general_group())
        body.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        # Final sync passes.
        self._sync_theme_options()
        self._sync_editor_custom_enabled()
        self._refresh_editor_detection()

    # ── Appearance ───────────────────────────────────────────────────

    def _build_appearance_group(self) -> QGroupBox:
        box = QGroupBox("APPEARANCE")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        # Theme picker
        self._theme_combo = QComboBox()
        self._theme_combo.addItems(ThemeManager.instance().theme_names())
        self._theme_combo.setCurrentText(ThemeManager.instance().current.name)
        self._theme_combo.setMinimumWidth(200)
        self._theme_combo.currentTextChanged.connect(self._on_theme_changed)
        form.addRow("Theme:", self._theme_combo)

        self._theme_hint = QLabel(
            "Dark · Neon · Space · Liquid Glass · Light (WinSCP) · "
            "OG Black · Retro Terminal."
        )
        self._theme_hint.setObjectName("lbl_subtitle")
        self._theme_hint.setWordWrap(True)
        form.addRow("", self._theme_hint)

        # Liquid Glass opacity
        glass_row = QWidget()
        gr = QHBoxLayout(glass_row)
        gr.setContentsMargins(0, 0, 0, 0)
        gr.setSpacing(10)
        self._glass_slider = QSlider(Qt.Orientation.Horizontal)
        self._glass_slider.setRange(60, 100)
        self._glass_slider.setValue(settings.get("glass_opacity", 88))
        self._glass_slider.setTickInterval(5)
        self._glass_slider.setMinimumWidth(200)
        self._glass_slider.valueChanged.connect(self._on_glass_opacity)
        gr.addWidget(self._glass_slider, 1)
        self._glass_label = QLabel(f"{self._glass_slider.value()}%")
        self._glass_label.setFixedWidth(42)
        gr.addWidget(self._glass_label)
        self._glass_row_label = QLabel("Background opacity:")
        form.addRow(self._glass_row_label, glass_row)
        self._glass_row_widget = glass_row

        # OG Black accent
        self._accent_combo = QComboBox()
        self._accent_combo.setMinimumWidth(160)
        for name, (color, *_rest) in OG_BLACK_ACCENTS.items():
            pm = QPixmap(14, 14)
            pm.fill(QColor(color))
            self._accent_combo.addItem(QIcon(pm), name)
        saved_accent = settings.get("og_accent", "Blue")
        idx = self._accent_combo.findText(saved_accent)
        if idx >= 0:
            self._accent_combo.setCurrentIndex(idx)
        self._accent_combo.currentTextChanged.connect(self._on_og_accent)
        self._accent_label = QLabel("Accent colour:")
        form.addRow(self._accent_label, self._accent_combo)

        return box

    def _on_theme_changed(self, name: str) -> None:
        ThemeManager.instance().set_theme(name)
        settings.set_value("theme", name)
        self._sync_theme_options()
        self.status_message.emit(f"Theme: {name}")

    def _on_glass_opacity(self, value: int) -> None:
        self._glass_label.setText(f"{value}%")
        ThemeManager.instance().set_glass_opacity(value)
        settings.set_value("glass_opacity", value)

    def _on_og_accent(self, name: str) -> None:
        ThemeManager.instance().set_og_accent(name)
        settings.set_value("og_accent", name)

    def _sync_theme_options(self) -> None:
        cur = self._theme_combo.currentText()
        is_glass = cur == "Liquid Glass"
        is_og = cur == "OG Black"
        self._glass_row_widget.setVisible(is_glass)
        self._glass_row_label.setVisible(is_glass)
        self._accent_combo.setVisible(is_og)
        self._accent_label.setVisible(is_og)

    # ── AI ───────────────────────────────────────────────────────────

    def _build_ai_group(self) -> QGroupBox:
        box = QGroupBox("AI ASSISTANT")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        cfg = load_ai_config()

        # Enable toggle
        self._ai_enabled = QCheckBox("Enable the AI assistant")
        self._ai_enabled.setChecked(bool(cfg.enabled))
        self._ai_enabled.toggled.connect(self._on_ai_dirty)
        form.addRow("", self._ai_enabled)

        # Provider picker
        self._ai_provider = QComboBox()
        self._ai_provider.addItem("Ollama  (local, no internet required)", "ollama")
        self._ai_provider.addItem("Groq  (free cloud, fast)", "groq")
        self._ai_provider.setMinimumWidth(260)
        idx = self._ai_provider.findData(cfg.provider or "ollama")
        if idx >= 0:
            self._ai_provider.setCurrentIndex(idx)
        self._ai_provider.currentIndexChanged.connect(self._on_ai_dirty)
        self._ai_provider.currentIndexChanged.connect(self._sync_ai_provider_fields)
        form.addRow("Provider:", self._ai_provider)

        # Dynamic hint — updated by _sync_ai_provider_fields
        self._ai_hint = QLabel()
        self._ai_hint.setObjectName("lbl_subtitle")
        self._ai_hint.setWordWrap(True)
        self._ai_hint.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("", self._ai_hint)

        # Ollama base URL (hidden when provider=groq)
        self._ai_url = QLineEdit(cfg.base_url)
        self._ai_url.setPlaceholderText("http://localhost:11434")
        self._ai_url.setMinimumWidth(260)
        self._ai_url.textChanged.connect(self._on_ai_dirty)
        self._ai_url_label = QLabel("Ollama base URL:")
        form.addRow(self._ai_url_label, self._ai_url)

        # Groq API key (hidden when provider=ollama)
        self._ai_groq_key = QLineEdit(cfg.groq_api_key)
        self._ai_groq_key.setPlaceholderText("gsk_…  (paste your key here)")
        self._ai_groq_key.setMinimumWidth(260)
        self._ai_groq_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._ai_groq_key.textChanged.connect(self._on_ai_dirty)
        self._ai_groq_key_label = QLabel("Groq API key:")
        form.addRow(self._ai_groq_key_label, self._ai_groq_key)

        # Default model — read-only reminder; real picker lives on
        # Assistant page so the dropdown stays in lockstep with the
        # model registry's live refresh.
        self._ai_model_label = QLabel(
            cfg.effective_model() or "(not configured)"
        )
        self._ai_model_label.setObjectName("settings_readonly_value")
        form.addRow("Active model:", self._ai_model_label)

        model_hint = QLabel(
            "Pick or switch models from the <b>Assistant</b> page — "
            "the dropdown there reflects available models and updates live."
        )
        model_hint.setObjectName("lbl_subtitle")
        model_hint.setWordWrap(True)
        model_hint.setTextFormat(Qt.TextFormat.RichText)
        self._ai_model_hint = model_hint
        form.addRow("", model_hint)

        # Timeout
        self._ai_timeout = QSpinBox()
        self._ai_timeout.setRange(5, 600)
        self._ai_timeout.setSuffix(" s")
        self._ai_timeout.setValue(int(cfg.timeout))
        self._ai_timeout.valueChanged.connect(self._on_ai_dirty)
        form.addRow("Request timeout:", self._ai_timeout)

        # Max tokens
        self._ai_max_tokens = QSpinBox()
        self._ai_max_tokens.setRange(64, 8192)
        self._ai_max_tokens.setSingleStep(64)
        self._ai_max_tokens.setValue(int(cfg.max_tokens))
        self._ai_max_tokens.valueChanged.connect(self._on_ai_dirty)
        form.addRow("Max tokens:", self._ai_max_tokens)

        # Temperature
        self._ai_temperature = QDoubleSpinBox()
        self._ai_temperature.setRange(0.0, 2.0)
        self._ai_temperature.setSingleStep(0.05)
        self._ai_temperature.setDecimals(2)
        self._ai_temperature.setValue(float(cfg.temperature))
        self._ai_temperature.valueChanged.connect(self._on_ai_dirty)
        form.addRow("Temperature:", self._ai_temperature)

        # Apply / revert buttons
        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(8)

        self._ai_status_label = QLabel("Saved.")
        self._ai_status_label.setObjectName("settings_dirty_label")
        br.addWidget(self._ai_status_label, 1, Qt.AlignmentFlag.AlignVCenter)

        self._btn_ai_revert = QPushButton("Revert")
        self._btn_ai_revert.setObjectName("btn_action")
        self._btn_ai_revert.clicked.connect(self._on_ai_revert)
        br.addWidget(self._btn_ai_revert)

        self._btn_ai_apply = QPushButton("Apply AI changes")
        self._btn_ai_apply.setObjectName("btn_primary")
        self._btn_ai_apply.clicked.connect(self._on_ai_apply)
        br.addWidget(self._btn_ai_apply)

        form.addRow("", btn_row)

        self._ai_dirty = False
        self._ai_loaded_cfg = cfg
        self._refresh_ai_button_state()
        self._sync_ai_provider_fields()

        return box

    def _sync_ai_provider_fields(self, *_args) -> None:
        """Show/hide provider-specific rows and update the hint text."""
        is_groq = self._ai_provider.currentData() == "groq"

        if is_groq:
            self._ai_hint.setText(
                "Uses <b>Groq Cloud</b> for fast, free AI inference. "
                "Your messages are sent to Groq's servers. "
                "Get a free key at <code>console.groq.com</code> — "
                "no credit card required."
            )
        else:
            self._ai_hint.setText(
                "Uses a local <b>Ollama</b> daemon. Nothing ever leaves "
                "your machine. Install with "
                "<code>ollama pull llama3.2:3b</code>."
            )

        self._ai_url_label.setVisible(not is_groq)
        self._ai_url.setVisible(not is_groq)
        self._ai_groq_key_label.setVisible(is_groq)
        self._ai_groq_key.setVisible(is_groq)

    def _on_ai_dirty(self, *_args) -> None:
        self._ai_dirty = True
        self._refresh_ai_button_state()

    def _refresh_ai_button_state(self) -> None:
        dirty = self._ai_dirty
        self._btn_ai_apply.setEnabled(dirty)
        self._btn_ai_revert.setEnabled(dirty)
        self._ai_status_label.setText(
            "Unsaved changes — press Apply to use them."
            if dirty else "Saved."
        )

    def _on_ai_revert(self) -> None:
        cfg = self._ai_loaded_cfg
        for w in (
            self._ai_enabled, self._ai_provider,
            self._ai_url, self._ai_groq_key,
            self._ai_timeout, self._ai_max_tokens, self._ai_temperature,
        ):
            w.blockSignals(True)
        try:
            self._ai_enabled.setChecked(bool(cfg.enabled))
            idx = self._ai_provider.findData(cfg.provider or "ollama")
            if idx >= 0:
                self._ai_provider.setCurrentIndex(idx)
            self._ai_url.setText(cfg.base_url)
            self._ai_groq_key.setText(cfg.groq_api_key)
            self._ai_timeout.setValue(int(cfg.timeout))
            self._ai_max_tokens.setValue(int(cfg.max_tokens))
            self._ai_temperature.setValue(float(cfg.temperature))
        finally:
            for w in (
                self._ai_enabled, self._ai_provider,
                self._ai_url, self._ai_groq_key,
                self._ai_timeout, self._ai_max_tokens, self._ai_temperature,
            ):
                w.blockSignals(False)
        self._sync_ai_provider_fields()
        self._ai_dirty = False
        self._refresh_ai_button_state()

    def _on_ai_apply(self) -> None:
        if not self._ai_dirty:
            return
        current = load_ai_config()
        from dataclasses import replace
        new_cfg = replace(
            current,
            enabled=self._ai_enabled.isChecked(),
            provider=self._ai_provider.currentData() or "ollama",
            base_url=self._ai_url.text().strip() or current.base_url,
            groq_api_key=self._ai_groq_key.text().strip(),
            timeout=int(self._ai_timeout.value()),
            max_tokens=int(self._ai_max_tokens.value()),
            temperature=float(self._ai_temperature.value()),
        )
        # If we have a live service reference, rotate the running
        # instance — this also persists via save_config. Otherwise
        # just persist directly.
        applied_live = False
        if self._ai_service_ref is not None:
            try:
                self._ai_service_ref.update_config(new_cfg)
                applied_live = True
            except Exception:
                applied_live = False
        if not applied_live:
            from ai.model_config import save_config
            try:
                save_config(new_cfg)
            except Exception as exc:
                QMessageBox.critical(
                    self, "Settings",
                    f"Could not save AI settings:\n{exc}"
                )
                return

        self._ai_loaded_cfg = new_cfg
        self._ai_dirty = False
        self._refresh_ai_button_state()
        self._ai_model_label.setText(
            new_cfg.effective_model() or "(not configured)"
        )
        self.status_message.emit("AI settings applied.")

    # ── Editor ───────────────────────────────────────────────────────

    def _build_editor_group(self) -> QGroupBox:
        box = QGroupBox("FILE OPEN EDITOR")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        self._editor_combo = QComboBox()
        for code in _ed.PREF_ORDER:
            self._editor_combo.addItem(_ed.PREF_LABELS[code], code)
        self._editor_combo.setMinimumWidth(300)
        current_pref, current_custom = _ed.get_editor_preference()
        idx = self._editor_combo.findData(current_pref)
        if idx >= 0:
            self._editor_combo.setCurrentIndex(idx)
        self._editor_combo.currentIndexChanged.connect(
            self._on_editor_pref_changed
        )
        form.addRow("Preferred editor:", self._editor_combo)

        hint = QLabel(
            "Used by <b>File Transfer</b> when you Open a remote file. "
            "Auto tries Notepad++, Notepad, then the system default."
        )
        hint.setObjectName("lbl_subtitle")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.TextFormat.RichText)
        self._editor_hint = hint
        form.addRow("", hint)

        # Custom path row
        custom_row = QWidget()
        cr = QHBoxLayout(custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        self._editor_path = QLineEdit(current_custom or "")
        self._editor_path.setPlaceholderText(
            r"C:\Path\to\editor.exe"
        )
        self._editor_path.editingFinished.connect(self._on_editor_path_edited)
        cr.addWidget(self._editor_path, 1)
        self._editor_browse = QPushButton("Browse…")
        self._editor_browse.setObjectName("btn_action")
        self._editor_browse.clicked.connect(self._on_editor_browse)
        cr.addWidget(self._editor_browse)
        form.addRow("Custom path:", custom_row)

        # Detected row
        det_row = QWidget()
        dr = QHBoxLayout(det_row)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.setSpacing(10)
        self._editor_detect = QLabel("")
        self._editor_detect.setWordWrap(True)
        dr.addWidget(self._editor_detect, 1)
        self._btn_editor_refresh = QPushButton("Refresh detection")
        self._btn_editor_refresh.setObjectName("btn_action")
        self._btn_editor_refresh.clicked.connect(self._refresh_editor_detection)
        dr.addWidget(self._btn_editor_refresh)
        form.addRow("Detected:", det_row)

        return box

    def _on_editor_pref_changed(self, _idx: int) -> None:
        code = self._editor_combo.currentData() or _ed.PREF_AUTO
        _ed.set_editor_preference(code)
        self._sync_editor_custom_enabled()

    def _sync_editor_custom_enabled(self) -> None:
        code = self._editor_combo.currentData() or _ed.PREF_AUTO
        is_custom = (code == _ed.PREF_CUSTOM)
        self._editor_path.setEnabled(is_custom)
        self._editor_browse.setEnabled(is_custom)

    def _on_editor_path_edited(self) -> None:
        path = self._editor_path.text().strip()
        _ed.set_editor_preference(
            self._editor_combo.currentData() or _ed.PREF_AUTO,
            custom_path=path,
        )

    def _on_editor_browse(self) -> None:
        current = self._editor_path.text().strip()
        start_dir = os.path.dirname(current) if current else ""
        if platform.system() == "Windows":
            filt = "Executables (*.exe *.cmd *.bat);;All files (*)"
        else:
            filt = "All files (*)"
        path, _sel = QFileDialog.getOpenFileName(
            self, "Choose editor executable", start_dir, filt,
        )
        if not path:
            return
        self._editor_path.setText(path)
        _ed.set_editor_preference(
            self._editor_combo.currentData() or _ed.PREF_AUTO,
            custom_path=path,
        )

    def _refresh_editor_detection(self) -> None:
        _ed.clear_detection_cache()
        parts = []
        for label, finder in (
            ("Notepad++", _ed.find_notepadpp),
            ("Notepad", _ed.find_notepad),
            ("VS Code", _ed.find_vscode),
        ):
            p = finder()
            parts.append(f"{label}: {'installed' if p else 'not installed'}")
        self._editor_detect.setText("   ·   ".join(parts))

    # ── Terminal ─────────────────────────────────────────────────────

    def _build_terminal_group(self) -> QGroupBox:
        box = QGroupBox("TERMINAL")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        self._shell_combo = QComboBox()
        self._shell_combo.setMinimumWidth(220)
        shells = available_shell_names()
        for sh in shells:
            self._shell_combo.addItem(sh, sh)
        saved = settings.get("terminal_shell", "") or default_shell_name()
        idx = self._shell_combo.findData(saved)
        if idx < 0 and self._shell_combo.count() > 0:
            idx = 0
        if idx >= 0:
            self._shell_combo.setCurrentIndex(idx)
        self._shell_combo.currentIndexChanged.connect(self._on_shell_changed)
        form.addRow("Default shell:", self._shell_combo)

        hint = QLabel(
            "Shell used when the Terminal page opens. Only shells "
            "installed on this machine are listed. Live terminal "
            "sessions keep their current shell — the change applies "
            "next time a terminal starts."
        )
        hint.setObjectName("lbl_subtitle")
        hint.setWordWrap(True)
        self._terminal_hint = hint
        form.addRow("", hint)

        return box

    def _on_shell_changed(self, _idx: int) -> None:
        target = self._shell_combo.currentData()
        if not target:
            return
        settings.set_value("terminal_shell", target)
        self.status_message.emit(f"Default shell: {target}")

    # ── File Transfer ────────────────────────────────────────────────

    def _build_transfer_group(self) -> QGroupBox:
        box = QGroupBox("FILE TRANSFER")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        self._ft_local_collapsed = QCheckBox(
            "Remember local pane collapsed state"
        )
        self._ft_local_collapsed.setChecked(
            bool(settings.get("file_transfer_local_collapsed", False))
        )
        self._ft_local_collapsed.toggled.connect(self._on_ft_collapse_toggled)
        form.addRow("", self._ft_local_collapsed)

        hint = QLabel(
            "When checked, the File Transfer page reopens with the "
            "local pane collapsed to match your last session. Uncheck "
            "to always start with both panes visible."
        )
        hint.setObjectName("lbl_subtitle")
        hint.setWordWrap(True)
        self._ft_hint = hint
        form.addRow("", hint)

        info = QLabel(
            "Remote files you Open are staged to a per-session temp "
            "cache and cleaned up when the app exits. Downloads go to "
            "the path you pick in the save dialog — they are not "
            "cached."
        )
        info.setObjectName("lbl_subtitle")
        info.setWordWrap(True)
        self._ft_info = info
        form.addRow("Behaviour:", info)

        return box

    def _on_ft_collapse_toggled(self, on: bool) -> None:
        settings.set_value("file_transfer_local_collapsed", bool(on))

    # ── General ──────────────────────────────────────────────────────

    def _build_general_group(self) -> QGroupBox:
        box = QGroupBox("GENERAL")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setContentsMargins(18, 24, 18, 18)

        path_row = QWidget()
        pr = QHBoxLayout(path_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(8)

        self._settings_path_value = QLineEdit(settings.settings_file_path())
        self._settings_path_value.setReadOnly(True)
        pr.addWidget(self._settings_path_value, 1)

        self._btn_open_folder = QPushButton("Open folder")
        self._btn_open_folder.setObjectName("btn_action")
        self._btn_open_folder.clicked.connect(self._on_open_settings_folder)
        pr.addWidget(self._btn_open_folder)

        form.addRow("Settings file:", path_row)

        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.setSpacing(8)
        br.addStretch(1)

        self._btn_reset = QPushButton("Reset to defaults…")
        self._btn_reset.setObjectName("btn_danger")
        self._btn_reset.clicked.connect(self._on_reset_all)
        br.addWidget(self._btn_reset)

        form.addRow("", btn_row)

        hint = QLabel(
            "Resetting overwrites the settings file with built-in "
            "defaults. Live values already loaded in the app are not "
            "rewritten — restart Net Engine to pick them all up."
        )
        hint.setObjectName("lbl_subtitle")
        hint.setWordWrap(True)
        self._gen_hint = hint
        form.addRow("", hint)

        return box

    def _on_open_settings_folder(self) -> None:
        path = settings.settings_dir_path()
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        try:
            if platform.system() == "Windows":
                os.startfile(path)  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            QMessageBox.warning(
                self, "Settings",
                f"Could not open settings folder:\n{exc}"
            )

    def _on_reset_all(self) -> None:
        resp = QMessageBox.question(
            self, "Reset settings",
            "Reset all saved preferences to their defaults?\n\n"
            "The current settings file will be overwritten. Saved "
            "SSH hosts and IP profiles will be cleared. This cannot "
            "be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            settings.reset_all()
        except Exception as exc:
            QMessageBox.critical(
                self, "Reset settings",
                f"Could not reset settings:\n{exc}"
            )
            return
        QMessageBox.information(
            self, "Reset settings",
            "Defaults restored. Restart Net Engine to pick up every "
            "change cleanly."
        )
        self.status_message.emit("Settings reset to defaults.")

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_entered(self) -> None:
        # Refresh dynamic readouts each time the page is shown.
        self._refresh_editor_detection()

    def shutdown(self) -> None:
        pass

    # ── Theme ───────────────────────────────────────────────────────

    def _restyle(self, t) -> None:
        accent2 = t.accent2 or t.accent
        self.setStyleSheet(
            f"#settings_header {{"
            f"  background-color: {t.bg_raised};"
            f"  border-bottom: 1px solid {t.border_lt};"
            f"}}"
            f"#settings_title {{"
            f"  color: {t.accent};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 20px;"
            f"  font-weight: 900;"
            f"  letter-spacing: 2.4px;"
            f"}}"
            f"#settings_path {{"
            f"  color: {t.text_dim};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 11px;"
            f"}}"
            f"#settings_scroll {{"
            f"  background-color: {t.bg_base};"
            f"  border: none;"
            f"}}"
            f"#settings_content {{"
            f"  background-color: {t.bg_base};"
            f"}}"
            f"#settings_readonly_value {{"
            f"  color: {t.text};"
            f"  font-family: 'JetBrains Mono','Consolas',monospace;"
            f"  font-size: 12px;"
            f"}}"
            f"#settings_dirty_label {{"
            f"  color: {accent2};"
            f"  font-size: 11px;"
            f"  font-style: italic;"
            f"}}"
        )
