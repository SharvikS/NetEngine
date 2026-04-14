"""
Host table — QAbstractTableModel + QTableView with custom delegates.
Supports sorting, filtering, and inline status/latency coloring.
"""

from __future__ import annotations

from PyQt6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
    pyqtSignal, QVariant,
)
from PyQt6.QtGui import QColor, QFont, QBrush
from PyQt6.QtWidgets import (
    QTableView, QHeaderView, QAbstractItemView,
)

from scanner.host_scanner import HostInfo
from gui.themes import theme, ThemeManager


# ── Column definitions ────────────────────────────────────────────────────────

COLUMNS = [
    ("IP Address",  "ip"),
    ("Hostname",    "hostname"),
    ("MAC Address", "mac"),
    ("Vendor",      "vendor"),
    ("Status",      "status"),
    ("Latency",     "latency_ms"),
    ("Open Ports",  "open_ports"),
    ("OS Hint",     "os_hint"),
]

COL_IP       = 0
COL_HOSTNAME = 1
COL_MAC      = 2
COL_VENDOR   = 3
COL_STATUS   = 4
COL_LATENCY  = 5
COL_PORTS    = 6
COL_OS       = 7


# ── Data model ────────────────────────────────────────────────────────────────

class HostTableModel(QAbstractTableModel):
    """Holds all HostInfo objects and presents them to the view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hosts: list[HostInfo] = []
        self._ip_index: dict[str, int] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def upsert(self, info: HostInfo):
        if info.ip in self._ip_index:
            row = self._ip_index[info.ip]
            self._hosts[row] = info
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, len(COLUMNS) - 1),
            )
        else:
            row = len(self._hosts)
            self.beginInsertRows(QModelIndex(), row, row)
            self._hosts.append(info)
            self._ip_index[info.ip] = row
            self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self._hosts.clear()
        self._ip_index.clear()
        self.endResetModel()

    def host_at(self, row: int) -> HostInfo | None:
        if 0 <= row < len(self._hosts):
            return self._hosts[row]
        return None

    @property
    def hosts(self) -> list[HostInfo]:
        return list(self._hosts)

    def alive_count(self) -> int:
        return sum(1 for h in self._hosts if h.is_alive)

    # ── QAbstractTableModel overrides ─────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._hosts)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section][0]
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(QColor(theme().text_dim))
        return QVariant()

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return QVariant()

        row, col = index.row(), index.column()
        if row >= len(self._hosts):
            return QVariant()

        host = self._hosts[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(host, col)

        if role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(QColor(self._fg_color(host, col)))

        if role == Qt.ItemDataRole.FontRole:
            if col in (COL_IP, COL_MAC, COL_LATENCY):
                f = QFont("Consolas", 11)
                f.setFixedPitch(True)
                return f
            if col == COL_STATUS:
                f = QFont("Segoe UI", 10)
                f.setBold(True)
                return f

        if role == Qt.ItemDataRole.UserRole:
            return host

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_STATUS:
                return Qt.AlignmentFlag.AlignCenter
            if col == COL_LATENCY:
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        return QVariant()

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder):
        self.layoutAboutToBeChanged.emit()
        key, reverse = self._sort_key(column), (order == Qt.SortOrder.DescendingOrder)
        self._hosts.sort(key=key, reverse=reverse)
        self._ip_index = {h.ip: i for i, h in enumerate(self._hosts)}
        self.layoutChanged.emit()

    def refresh_colors(self):
        """Notify view that all colors should be re-fetched (theme change)."""
        if self._hosts:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._hosts) - 1, len(COLUMNS) - 1),
                [Qt.ItemDataRole.ForegroundRole],
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _display(self, host: HostInfo, col: int) -> str:
        if col == COL_IP:       return host.ip
        if col == COL_HOSTNAME: return host.hostname or "—"
        if col == COL_MAC:      return host.mac or "—"
        if col == COL_VENDOR:   return host.vendor or "—"
        if col == COL_STATUS:   return host.status.upper()
        if col == COL_LATENCY:  return host.latency_display
        if col == COL_PORTS:    return host.ports_display
        if col == COL_OS:       return host.os_hint or "—"
        return ""

    def _fg_color(self, host: HostInfo, col: int) -> str:
        t = theme()
        if col == COL_STATUS:
            return t.status_colors.get(host.status, t.text_dim)
        if col == COL_LATENCY:
            return t.latency_color(host.latency_ms)
        if col == COL_IP:
            return t.text_mono
        if col == COL_MAC:
            return t.text_dim
        if col == COL_PORTS:
            return t.accent if host.open_ports else t.text_dim
        return t.text

    def _sort_key(self, col: int):
        import ipaddress
        if col == COL_IP:
            def k(h):
                try:
                    return int(ipaddress.ip_address(h.ip))
                except Exception:
                    return 0
            return k
        if col == COL_HOSTNAME: return lambda h: h.hostname.lower()
        if col == COL_MAC:      return lambda h: h.mac
        if col == COL_VENDOR:   return lambda h: h.vendor.lower()
        if col == COL_STATUS:   return lambda h: h.status
        if col == COL_LATENCY:  return lambda h: h.latency_ms if h.latency_ms >= 0 else 9999
        if col == COL_PORTS:    return lambda h: len(h.open_ports)
        if col == COL_OS:       return lambda h: h.os_hint.lower()
        return lambda h: h.ip


# ── Proxy model for search/filter ─────────────────────────────────────────────

class HostFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_text = ""
        self._status_filter = "all"
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_filter_text(self, text: str):
        self._filter_text = text.strip().lower()
        self.invalidateFilter()

    def set_status_filter(self, status: str):
        self._status_filter = status
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model: HostTableModel = self.sourceModel()
        host = model.host_at(source_row)
        if host is None:
            return False

        if self._status_filter == "alive" and not host.is_alive:
            return False
        if self._status_filter == "dead" and host.is_alive:
            return False

        if self._filter_text:
            haystack = " ".join([
                host.ip, host.hostname, host.mac, host.vendor, host.os_hint,
                " ".join(str(p) for p in host.open_ports),
            ]).lower()
            if self._filter_text not in haystack:
                return False

        return True


# ── Main table widget ─────────────────────────────────────────────────────────

class HostTableWidget(QTableView):
    """The primary host table."""

    host_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)

        self._source_model = HostTableModel(self)
        self._proxy = HostFilterProxy(self)
        self._proxy.setSourceModel(self._source_model)
        self.setModel(self._proxy)

        self._setup_view()
        ThemeManager.instance().theme_changed.connect(self._on_theme_changed)

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _setup_view(self):
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(COL_IP,        QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_HOSTNAME,  QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_MAC,       QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_VENDOR,    QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(COL_STATUS,    QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_LATENCY,   QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(COL_PORTS,     QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_OS,        QHeaderView.ResizeMode.Interactive)
        hdr.setHighlightSections(False)
        hdr.setMinimumSectionSize(80)
        hdr.setStretchLastSection(False)

        # Generous, fully-visible widths for fixed columns
        self.setColumnWidth(COL_IP,      150)
        self.setColumnWidth(COL_MAC,     170)
        self.setColumnWidth(COL_STATUS,  100)
        self.setColumnWidth(COL_LATENCY, 110)
        self.setColumnWidth(COL_VENDOR,  170)
        self.setColumnWidth(COL_OS,      130)

        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(38)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)

        self.sortByColumn(COL_IP, Qt.SortOrder.AscendingOrder)
        self.selectionModel().selectionChanged.connect(self._on_selection)

    # ── Public API ────────────────────────────────────────────────────────────

    def upsert_host(self, info: HostInfo):
        self._source_model.upsert(info)

    def clear_hosts(self):
        self._source_model.clear()

    def set_filter(self, text: str):
        self._proxy.set_filter_text(text)

    def set_status_filter(self, status: str):
        self._proxy.set_status_filter(status)

    def get_all_hosts(self) -> list[HostInfo]:
        return self._source_model.hosts

    def alive_count(self) -> int:
        return self._source_model.alive_count()

    def total_count(self) -> int:
        return self._source_model.rowCount()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_selection(self):
        rows = self.selectionModel().selectedRows()
        if rows:
            src_idx = self._proxy.mapToSource(rows[0])
            host = self._source_model.host_at(src_idx.row())
            self.host_selected.emit(host)
        else:
            self.host_selected.emit(None)

    def _on_theme_changed(self, _t):
        self._source_model.refresh_colors()
        self.viewport().update()
