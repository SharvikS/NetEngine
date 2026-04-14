"""
API Console page — built-in REST client.

A focused HTTP client for poking embedded device APIs without leaving
Net Engine. Supports:

  * GET / POST / PUT / PATCH / DELETE
  * Custom headers (key/value table)
  * JSON or raw body
  * Basic and Bearer authentication
  * Save / load named requests (persisted in Net Engine settings)
  * Import / export of cURL command strings

The actual HTTP work runs on a worker thread; the UI never blocks. If
the `requests` library is not installed the page presents a clean,
informative banner instead of failing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QPlainTextEdit, QSplitter, QTabWidget, QFileDialog,
    QMessageBox, QInputDialog, QFrame,
)

from gui.themes import theme, ThemeManager
from utils import settings


try:
    import requests           # type: ignore
    _HAS_REQUESTS = True
except ImportError:           # pragma: no cover
    requests = None           # type: ignore
    _HAS_REQUESTS = False


# ── HTTP worker ──────────────────────────────────────────────────────────────


class _ApiWorker(QThread):
    """Run a single HTTP request on a worker thread."""

    done = pyqtSignal(dict)

    def __init__(self, request_obj: dict, parent=None):
        super().__init__(parent)
        self.req = request_obj

    def run(self) -> None:
        if not _HAS_REQUESTS:
            self.done.emit({
                "ok": False,
                "status": "REQUESTS_MISSING",
                "elapsed_ms": 0,
                "headers": {},
                "body": (
                    "The 'requests' library is not installed.\n"
                    "Run: pip install requests"
                ),
            })
            return

        method = self.req.get("method", "GET").upper()
        url = self.req.get("url", "")
        headers = dict(self.req.get("headers") or {})
        body = self.req.get("body", "")
        auth_type = self.req.get("auth_type", "None")
        user = self.req.get("auth_user", "")
        pw = self.req.get("auth_pass", "")
        token = self.req.get("token", "")

        auth = None
        if auth_type == "Basic":
            auth = (user, pw)
        elif auth_type == "Bearer" and token:
            headers["Authorization"] = f"Bearer {token}"

        kwargs: dict[str, Any] = {
            "headers": headers,
            "auth": auth,
            "timeout": 30,
            "verify": False,
        }
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            ct = headers.get("Content-Type", "")
            if "application/json" in ct:
                try:
                    kwargs["json"] = json.loads(body) if body.strip() else {}
                except Exception:
                    kwargs["data"] = body
            else:
                kwargs["data"] = body

        try:
            start = time.time()
            resp = requests.request(method, url, **kwargs)
            elapsed_ms = int((time.time() - start) * 1000)
            try:
                parsed = resp.json()
                body_out = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                body_out = resp.text
            self.done.emit({
                "ok": True,
                "status": f"{resp.status_code} {resp.reason}",
                "elapsed_ms": elapsed_ms,
                "headers": dict(resp.headers),
                "body": body_out,
            })
        except Exception as exc:
            self.done.emit({
                "ok": False,
                "status": "ERROR",
                "elapsed_ms": 0,
                "headers": {},
                "body": f"{type(exc).__name__}: {exc}",
            })


# ── View ─────────────────────────────────────────────────────────────────────


class ApiConsoleView(QWidget):
    """Tabbed REST client embedded inside Net Engine."""

    status_message = pyqtSignal(str)

    METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
    AUTH_TYPES = ("None", "Basic", "Bearer")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: Optional[_ApiWorker] = None
        self._build_ui()
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self._restyle(theme())

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)

        title = QLabel("API CONSOLE")
        title.setObjectName("lbl_section")
        root.addWidget(title)

        subtitle = QLabel(
            "REST client for probing device APIs. "
            "Supports JSON, headers, Basic / Bearer auth and cURL import."
        )
        subtitle.setObjectName("lbl_subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        if not _HAS_REQUESTS:
            warn = QLabel(
                "  ⚠  The 'requests' Python package is required for the "
                "API console. Install it with `pip install requests` and "
                "restart Net Engine."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet(
                f"color: {theme().amber}; font-size: 12px;"
                f" padding: 10px; background-color: {theme().bg_input};"
                f" border: 1px solid {theme().border}; border-radius: 6px;"
            )
            root.addWidget(warn)

        # ── Request bar ─────────────────────────────────────────────────────
        bar = QFrame()
        bar.setObjectName("api_bar")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(12, 10, 12, 10)
        bar_lay.setSpacing(8)

        self._method = QComboBox()
        self._method.addItems(self.METHODS)
        self._method.setMinimumWidth(96)
        self._method.setMinimumHeight(32)
        bar_lay.addWidget(self._method)

        self._url = QLineEdit()
        self._url.setPlaceholderText("https://192.168.1.1/api/path")
        self._url.setMinimumHeight(32)
        bar_lay.addWidget(self._url, stretch=1)

        self._btn_send = QPushButton("Send")
        self._btn_send.setObjectName("btn_primary")
        self._btn_send.setMinimumHeight(32)
        self._btn_send.setMinimumWidth(96)
        self._btn_send.clicked.connect(self._on_send)
        if not _HAS_REQUESTS:
            self._btn_send.setEnabled(False)
        bar_lay.addWidget(self._btn_send)

        root.addWidget(bar)

        # ── Save / load / cURL strip ─────────────────────────────────────────
        action_strip = QHBoxLayout()
        action_strip.setSpacing(8)
        for label, slot in (
            ("Save Request",   self._on_save),
            ("Load Request…",  self._on_load),
            ("Import cURL",    self._on_import_curl),
            ("Export cURL",    self._on_export_curl),
        ):
            b = QPushButton(label)
            b.setObjectName("btn_action")
            b.setMinimumHeight(30)
            b.clicked.connect(slot)
            action_strip.addWidget(b)
        action_strip.addStretch()
        root.addLayout(action_strip)

        # ── Splitter: request editor (left) / response (right) ───────────────
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(2)

        # ── Left: request editor (auth, headers, body) ───────────────────────
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 8, 0)
        left_lay.setSpacing(12)

        # Auth row
        auth_box = QGroupBox("AUTHENTICATION")
        auth_lay = QHBoxLayout(auth_box)
        auth_lay.setContentsMargins(16, 24, 16, 14)
        auth_lay.setSpacing(8)

        auth_lay.addWidget(QLabel("Type"))
        self._auth_type = QComboBox()
        self._auth_type.addItems(self.AUTH_TYPES)
        self._auth_type.setMinimumWidth(110)
        self._auth_type.setMinimumHeight(30)
        auth_lay.addWidget(self._auth_type)

        self._auth_user = QLineEdit()
        self._auth_user.setPlaceholderText("user")
        self._auth_user.setMinimumHeight(30)
        auth_lay.addWidget(self._auth_user, stretch=1)

        self._auth_pass = QLineEdit()
        self._auth_pass.setPlaceholderText("password / token")
        self._auth_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._auth_pass.setMinimumHeight(30)
        auth_lay.addWidget(self._auth_pass, stretch=1)

        left_lay.addWidget(auth_box)

        # Headers
        headers_box = QGroupBox("HEADERS")
        h_lay = QVBoxLayout(headers_box)
        h_lay.setContentsMargins(16, 24, 16, 14)
        h_lay.setSpacing(8)

        self._headers = QTableWidget(0, 2)
        self._headers.setHorizontalHeaderLabels(["Key", "Value"])
        self._headers.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._headers.verticalHeader().setVisible(False)
        self._headers.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._headers.setMinimumHeight(120)
        h_lay.addWidget(self._headers, stretch=1)

        h_btn_row = QHBoxLayout()
        h_btn_row.setSpacing(8)
        b_add = QPushButton("Add Row")
        b_add.setObjectName("btn_action")
        b_add.setMinimumHeight(28)
        b_add.clicked.connect(self._on_add_header)
        h_btn_row.addWidget(b_add)

        b_del = QPushButton("Remove Row")
        b_del.setObjectName("btn_action")
        b_del.setMinimumHeight(28)
        b_del.clicked.connect(self._on_del_header)
        h_btn_row.addWidget(b_del)
        h_btn_row.addStretch()
        h_lay.addLayout(h_btn_row)

        left_lay.addWidget(headers_box, stretch=1)

        # Body
        body_box = QGroupBox("BODY")
        b_lay = QVBoxLayout(body_box)
        b_lay.setContentsMargins(16, 24, 16, 14)

        self._body = QPlainTextEdit()
        self._body.setPlaceholderText('{\n  "key": "value"\n}')
        f = QFont("Consolas", 11)
        f.setFixedPitch(True)
        self._body.setFont(f)
        self._body.setMinimumHeight(120)
        b_lay.addWidget(self._body)
        left_lay.addWidget(body_box, stretch=1)

        split.addWidget(left)

        # ── Right: response ─────────────────────────────────────────────────
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 0, 0, 0)
        right_lay.setSpacing(12)

        status_box = QGroupBox("RESPONSE STATUS")
        s_lay = QHBoxLayout(status_box)
        s_lay.setContentsMargins(16, 24, 16, 14)
        s_lay.setSpacing(12)
        self._status_value = QLabel("—")
        self._status_value.setStyleSheet(
            f"font-size: 14px; font-weight: 800;"
            f" color: {theme().text_dim};"
        )
        self._status_time  = QLabel("0 ms")
        self._status_time.setStyleSheet(
            f"font-size: 12px; color: {theme().text_dim};"
        )
        s_lay.addWidget(self._status_value)
        s_lay.addStretch()
        s_lay.addWidget(self._status_time)
        right_lay.addWidget(status_box)

        self._resp_tabs = QTabWidget()
        self._resp_body = QPlainTextEdit()
        self._resp_body.setReadOnly(True)
        self._resp_body.setFont(f)
        self._resp_tabs.addTab(self._resp_body, "Body")

        self._resp_headers = QPlainTextEdit()
        self._resp_headers.setReadOnly(True)
        self._resp_headers.setFont(f)
        self._resp_tabs.addTab(self._resp_headers, "Headers")

        right_lay.addWidget(self._resp_tabs, stretch=1)

        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([520, 600])

        root.addWidget(split, stretch=1)

    # ── Theme ────────────────────────────────────────────────────────────────

    def _restyle(self, t):
        self._status_value.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {t.text_dim};"
        )
        self._status_time.setStyleSheet(
            f"font-size: 12px; color: {t.text_dim};"
        )

    # ── Request collection / send ────────────────────────────────────────────

    def _collect(self) -> dict:
        headers = {}
        for row in range(self._headers.rowCount()):
            ki = self._headers.item(row, 0)
            vi = self._headers.item(row, 1)
            k = (ki.text() if ki else "").strip()
            v = (vi.text() if vi else "").strip()
            if k:
                headers[k] = v
        auth_type = self._auth_type.currentText()
        auth_user = self._auth_user.text().strip() if auth_type == "Basic" else ""
        auth_pass = self._auth_pass.text() if auth_type == "Basic" else ""
        token = self._auth_pass.text().strip() if auth_type == "Bearer" else ""
        return {
            "method": self._method.currentText(),
            "url": self._url.text().strip(),
            "auth_type": auth_type,
            "auth_user": auth_user,
            "auth_pass": auth_pass,
            "token": token,
            "headers": headers,
            "body": self._body.toPlainText(),
        }

    def _on_send(self):
        if not _HAS_REQUESTS:
            QMessageBox.critical(
                self, "API console",
                "The 'requests' Python package is required."
            )
            return
        req = self._collect()
        if not req["url"]:
            QMessageBox.warning(self, "API console", "URL is required.")
            return

        self._status_value.setText("RUNNING…")
        self._status_value.setStyleSheet(
            f"font-size: 14px; font-weight: 800; color: {theme().amber};"
        )
        self._status_time.setText("…")
        self._resp_body.clear()
        self._resp_headers.clear()
        self._btn_send.setEnabled(False)

        self._worker = _ApiWorker(req, self)
        self._worker.done.connect(self._on_done)
        self._worker.start()
        self.status_message.emit(f"API → {req['method']} {req['url']}")

    @pyqtSlot(dict)
    def _on_done(self, result: dict):
        self._btn_send.setEnabled(True)
        ok = result.get("ok", False)
        t = theme()
        self._status_value.setText(result.get("status", "—"))
        self._status_value.setStyleSheet(
            f"font-size: 14px; font-weight: 800;"
            f" color: {t.green if ok else t.red};"
        )
        self._status_time.setText(f"{result.get('elapsed_ms', 0)} ms")
        self._resp_body.setPlainText(result.get("body", ""))
        try:
            self._resp_headers.setPlainText(
                json.dumps(result.get("headers", {}), indent=2)
            )
        except Exception:
            self._resp_headers.setPlainText(str(result.get("headers", "")))

    # ── Headers ──────────────────────────────────────────────────────────────

    def _on_add_header(self):
        row = self._headers.rowCount()
        self._headers.insertRow(row)
        self._headers.setItem(row, 0, QTableWidgetItem(""))
        self._headers.setItem(row, 1, QTableWidgetItem(""))

    def _on_del_header(self):
        row = self._headers.currentRow()
        if row >= 0:
            self._headers.removeRow(row)

    # ── Persistence ──────────────────────────────────────────────────────────

    def _saved_requests(self) -> list[dict]:
        return list(settings.get("api_requests", []) or [])

    def _on_save(self):
        name, ok = QInputDialog.getText(
            self, "Save request", "Name for this request:"
        )
        if not ok or not name.strip():
            return
        req = self._collect()
        req["name"] = name.strip()
        all_reqs = [r for r in self._saved_requests() if r.get("name") != name]
        all_reqs.append(req)
        all_reqs.sort(key=lambda r: r.get("name", "").lower())
        settings.set_value("api_requests", all_reqs)
        self.status_message.emit(f"Saved API request '{name}'")

    def _on_load(self):
        reqs = self._saved_requests()
        if not reqs:
            QMessageBox.information(self, "Load request", "No saved requests yet.")
            return
        names = [r.get("name", "unnamed") for r in reqs]
        name, ok = QInputDialog.getItem(
            self, "Load request", "Select a request:", names, 0, False
        )
        if not ok or not name:
            return
        req = next((r for r in reqs if r.get("name") == name), None)
        if not req:
            return
        self._apply_request(req)
        self.status_message.emit(f"Loaded request '{name}'")

    def _apply_request(self, req: dict) -> None:
        self._method.setCurrentText(req.get("method", "GET"))
        self._url.setText(req.get("url", ""))
        self._auth_type.setCurrentText(req.get("auth_type", "None"))
        self._auth_user.setText(req.get("auth_user", ""))
        self._auth_pass.setText(req.get("auth_pass") or req.get("token", ""))
        self._body.setPlainText(req.get("body", ""))
        self._headers.setRowCount(0)
        for k, v in (req.get("headers") or {}).items():
            row = self._headers.rowCount()
            self._headers.insertRow(row)
            self._headers.setItem(row, 0, QTableWidgetItem(k))
            self._headers.setItem(row, 1, QTableWidgetItem(v))

    # ── cURL import / export ─────────────────────────────────────────────────

    def _on_import_curl(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "Import cURL", "Paste a cURL command:", ""
        )
        if not ok or not text.strip():
            return
        try:
            req = self._parse_curl(text.strip())
        except Exception as exc:
            QMessageBox.warning(self, "cURL parse", f"Could not parse: {exc}")
            return
        self._apply_request(req)
        self.status_message.emit("Imported cURL request")

    def _on_export_curl(self):
        req = self._collect()
        parts = [f'curl -X {req["method"]} "{req["url"]}"']
        for k, v in req["headers"].items():
            parts.append(f'-H "{k}: {v}"')
        if req["auth_type"] == "Bearer" and req["token"]:
            parts.append(f'-H "Authorization: Bearer {req["token"]}"')
        if req["auth_type"] == "Basic" and req["auth_user"]:
            parts.append(f'-u "{req["auth_user"]}:{req["auth_pass"]}"')
        if req["body"].strip():
            parts.append(f'-d "{req["body"]}"')
        curl = " \\\n  ".join(parts)
        self._resp_body.setPlainText(curl)
        self._resp_tabs.setCurrentWidget(self._resp_body)

    @staticmethod
    def _parse_curl(text: str) -> dict:
        method = "GET"
        m = re.search(r"-X\s+(\w+)", text)
        if m:
            method = m.group(1).upper()
        headers: dict[str, str] = {}
        for hm in re.findall(r"-H\s+['\"]([^:]+):\s*([^'\"]+)['\"]", text):
            headers[hm[0].strip()] = hm[1].strip()
        data_match = re.search(
            r"(?:--data|-d)\s+['\"](.*?)['\"]", text, re.DOTALL
        )
        body = data_match.group(1) if data_match else ""
        url_match = re.search(r"(https?://[^\s'\"]+)", text)
        url = url_match.group(1) if url_match else ""
        return {
            "method": method,
            "url": url,
            "auth_type": "None",
            "auth_user": "",
            "auth_pass": "",
            "token": "",
            "headers": headers,
            "body": body,
        }

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def shutdown(self):
        try:
            if self._worker is not None:
                self._worker.quit()
                self._worker.wait(500)
        except Exception:
            pass
