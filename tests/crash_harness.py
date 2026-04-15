"""
Headless crash-eradication harness.

Each scenario runs in its own subprocess so they're fully isolated
(no shared Qt/sip state across scenarios). A real user runs ONE
MainWindow per process, so this matches the production lifecycle
exactly: start the app → exercise one crash-prone flow → close.

Runs offscreen so no real display is required. A non-zero exit from
any scenario is reported as a failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HARNESS_PY = sys.executable

# Preamble that every scenario subprocess runs before its body.
PREAMBLE = """
import os, sys, time, threading
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, r%r)
from PyQt6.QtWidgets import QApplication
APP = QApplication.instance() or QApplication([])
from gui.motion import install_global_motion
install_global_motion(APP)
from gui.main_window import (
    MainWindow, PAGE_SCANNER, PAGE_TERMINAL, PAGE_SSH, PAGE_ADAPTER,
    PAGE_MONITOR, PAGE_TOOLS, PAGE_API, PAGE_ASSISTANT,
)
from scanner.ssh_client import SSHProfile

def pump(rounds=5):
    for _ in range(rounds):
        APP.processEvents()

def drain_ssh(timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [
            t for t in threading.enumerate()
            if t.name.startswith('ssh-connect-') and t.is_alive()
        ]
        if not alive:
            return
        time.sleep(0.05)

def fresh_window():
    w = MainWindow()
    w.show()
    pump(10)
    return w

def done(code=0):
    # os._exit bypasses interpreter cleanup so daemon paramiko threads
    # can't race with Python GC at exit.
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(code)
""" % ROOT


# ── Scenario bodies (strings, executed in subprocesses) ──────────────────────

SCENARIOS: dict[str, str] = {}


SCENARIOS["rapid_page_switch"] = """
w = fresh_window()
for _ in range(3):
    for pg in (PAGE_SCANNER, PAGE_TERMINAL, PAGE_SSH, PAGE_ADAPTER,
               PAGE_MONITOR, PAGE_TOOLS, PAGE_API, PAGE_ASSISTANT):
        w._switch_page(pg)
        pump(2)
w.close()
pump(20)
done()
"""

SCENARIOS["ssh_tab_spam"] = """
w = fresh_window()
w._switch_page(PAGE_SSH); pump()
ssh = w._ssh_view
ssh._open_session_tab(SSHProfile(
    name='t1', host='127.0.0.1', port=1, user='u', password='p'
))
pump()
tab = ssh._tabs.widget(0)
assert tab is not None

for _ in range(15):
    tab.reconnect()
    pump(2)
tab.disconnect_session(silent=False)
pump()

ssh._open_session_tab(SSHProfile(
    name='t2', host='127.0.0.1', port=1, user='u', password='p'
))
pump()
assert ssh._tabs.count() == 2

ssh._on_tab_close_requested(0)
ssh._on_tab_close_requested(0)
pump()
assert ssh._tabs.count() == 0

w.close()
pump(20)
drain_ssh()
done()
"""

SCENARIOS["ssh_focus_mode_spam"] = """
w = fresh_window()
w._switch_page(PAGE_SSH); pump()
ssh = w._ssh_view
ssh._open_session_tab(SSHProfile(
    name='f1', host='127.0.0.1', port=1, user='u', password='p'
))
pump()

for _ in range(20):
    ssh.toggle_terminal_focus_mode()
    pump(2)

if ssh.is_terminal_focus_mode():
    ssh.set_terminal_focus_mode(False)
pump()

w.close()
pump(20)
drain_ssh()
done()
"""

SCENARIOS["ssh_port_field_stress"] = """
w = fresh_window()
w._switch_page(PAGE_SSH); pump()

p = w._ssh_view._in_port
for value in ('', '2', '22', '22222', '99999', 'abc', '21'):
    p.clear()
    p.insert(value)
    pump()

# Stub the warning dialog so commit-time validation can run
# without popping a modal.
import gui.components.ssh_view as sv
sv.QMessageBox.warning = staticmethod(lambda *a, **k: None)

p.setText('')
assert w._ssh_view._validate_port() is None
p.setText('99999')
assert w._ssh_view._validate_port() is None
p.setText('8022')
assert w._ssh_view._validate_port() == 8022

w.close()
pump(20)
done()
"""

SCENARIOS["scanner_mid_scan_close"] = """
w = fresh_window()
w._switch_page(PAGE_SCANNER); pump()

sv = w._scanner_view
sv._start_scan({
    'network': '127.0.0.1',
    'cidr': 32,
    'ports': [],
    'max_workers': 4,
})
pump(2)
w.close()
pump(20)
done()
"""

SCENARIOS["monitor_shutdown"] = """
w = fresh_window()
w._switch_page(PAGE_MONITOR); pump()

mv = w._monitor_view
mv.add_target('127.0.0.1')
mv._on_start_all()
pump(2)
mv._on_stop_all()
pump()
w.close()
pump(20)
done()
"""

SCENARIOS["tools_command_close"] = """
w = fresh_window()
w._switch_page(PAGE_TOOLS); pump()
tv = w._tools_view
tv._run_command('echo hello', 'echo')
pump(3)
w.close()
pump(20)
done()
"""

SCENARIOS["api_console_shutdown"] = """
w = fresh_window()
w._switch_page(PAGE_API); pump()
av = w._api_view
av._url.setText('http://127.0.0.1:1/does-not-exist')
av._on_send()
# Close mid-request so the worker's late response lands in our
# hardened _on_done slot via queued connection (should drop).
pump(1)
w.close()
pump(20)
done()
"""

SCENARIOS["netconfig_shutdown"] = """
w = fresh_window()
w._switch_page(PAGE_ADAPTER); pump()
w._adapter_view.shutdown()
w.close()
pump(20)
done()
"""

SCENARIOS["sidebar_toggle_spam"] = """
w = fresh_window()
for _ in range(15):
    w._sidebar.toggle_compact()
    pump(1)
w.close()
pump(20)
done()
"""

SCENARIOS["assistant_copy_close"] = """
w = fresh_window()
w._switch_page(PAGE_ASSISTANT); pump()
av = w._assistant_view
av._cmd_line.setText('ls -la')
av._on_copy_command()
pump()
# Close before the 1500ms singleShot lambda fires — this is the
# exact original crash repro.
w.close()
pump(20)
done()
"""

SCENARIOS["detail_panel_copy_timer"] = """
from scanner.host_scanner import HostInfo
w = fresh_window()
w._switch_page(PAGE_SCANNER); pump()
d = w._scanner_view._detail
host = HostInfo(ip='10.0.0.5', status='alive')
d.show_host(host)
pump()
d._do_copy_ip()
pump()
# Close drawer before the 1500ms QTimer.singleShot lambda fires.
w.close()
pump(20)
done()
"""

SCENARIOS["focus_mode_close_during_connect"] = """
w = fresh_window()
w._switch_page(PAGE_SSH); pump()
ssh = w._ssh_view
ssh._open_session_tab(SSHProfile(
    name='c1', host='127.0.0.1', port=1, user='u', password='p'
))
pump()
# Enter focus mode while connect worker is in flight, then close.
ssh.set_terminal_focus_mode(True)
pump()
w.close()
pump(20)
drain_ssh()
done()
"""

SCENARIOS["ssh_saved_session_delete_real_modal"] = """
# Stronger regression: builds a real QMessageBox that goes through
# the motion-polish Show-event pathway on its Yes/No buttons.
# Instead of calling QMessageBox.question (which blocks on exec()),
# we manually build the dialog, post a Yes result via a zero-delay
# timer, and then destroy the dialog. This reproduces the exact
# pattern that was crashing the real app:
#
#   1. QMessageBox is constructed
#   2. Its child QPushButtons fire Show events
#   3. _NewWidgetWatcher polishes them with _GlowEffect +
#      QPropertyAnimation(blurRadius)
#   4. We close the dialog
#   5. Qt destroys the dialog and its children, including the
#      _GlowEffect
#   6. Any still-running QPropertyAnimation on that effect ticks
#      against a dangling C++ pointer => segfault
#
# The fix excludes QDialog children from polishing and parents
# animations to the effect (not the widget), so the animations die
# with the effect.
import gc
from PyQt6.QtCore import QTimer as _QTimer
from PyQt6.QtWidgets import QMessageBox as _QMessageBox

w = fresh_window()
w._switch_page(PAGE_SSH); pump()

# Build 10 real QMessageBoxes in rapid succession. Each one is
# shown, hovered (to trigger the motion filter's _press via
# mouseButtonPress-like events), then closed and destroyed.
# If motion.py incorrectly polishes dialog children, GC after
# destroying each dialog will crash the process.
for i in range(10):
    box = _QMessageBox(_QMessageBox.Icon.Question, f'test {i}',
                       f'message {i}', _QMessageBox.StandardButton.Yes |
                       _QMessageBox.StandardButton.No,
                       parent=w)
    box.show()
    # Pump so Qt fires Show events on the child buttons and the
    # motion _NewWidgetWatcher's eventFilter sees them. This is
    # where the polish would happen if we didn't skip QDialog
    # children.
    APP.processEvents()
    APP.processEvents()

    # Interact with the dialog's buttons. Find one and "press" it
    # via the motion filter's event pathway — if the filter
    # polished it, this schedules a QPropertyAnimation.
    from PyQt6.QtCore import Qt as _Qt
    yes_btn = None
    for btn in box.buttons():
        if box.buttonRole(btn) == _QMessageBox.ButtonRole.YesRole:
            yes_btn = btn
            break

    if yes_btn is not None:
        # Force mouse-like interaction so any attached motion
        # animations kick in before the dialog is destroyed.
        from PyQt6.QtGui import QEnterEvent
        from PyQt6.QtCore import QEvent, QPointF
        try:
            ev = QEnterEvent(QPointF(5, 5), QPointF(5, 5), QPointF(5, 5))
            APP.sendEvent(yes_btn, ev)
        except Exception:
            pass
        APP.processEvents()

    # Close + destroy the dialog.
    box.done(int(_QMessageBox.StandardButton.Yes))
    box.deleteLater()
    box = None
    yes_btn = None
    for _ in range(5):
        APP.processEvents()

    # Force GC so any lingering QPropertyAnimation on a destroyed
    # _GlowEffect is reaped right now (not at interpreter exit
    # where it would be a hidden crash).
    gc.collect()
    APP.processEvents()

w.close()
pump(20)
done()
"""


SCENARIOS["ssh_saved_session_delete"] = """
# Regression: the real user reported a crash when deleting a saved
# SSH session by clicking Yes in the confirm modal. The crash is
# caused by sip cleaning up a QListWidgetItem Python wrapper AFTER
# the C++ item has been destroyed by list.clear() inside
# _reload_sessions(). We stub QMessageBox.question to force the
# Yes path but also seed 5 sessions, select / delete them one by
# one, and pump events + run gc.collect() between operations so
# any dangling wrapper would actually crash the process.
import gc
from utils import settings
import gui.components.ssh_view as sv

# Clean slate for this scenario.
settings.set_value('ssh_hosts', [])

for i in range(5):
    settings.save_ssh_host({
        'name': f'harness-{i}',
        'host': f'10.20.30.{40 + i}',
        'port': 2222,
        'user': 'tester',
        'key_path': '',
        'auth_method': 'password',
        'save_credentials': False,
        'favorite': i == 0,
        'last_connected': '',
    })

# Stub the modal to immediately say Yes but still spin the Qt event
# loop for a moment — this mimics the real sequence where the event
# loop is active between the modal opening and closing.
def _fake_yes(*args, **kwargs):
    APP.processEvents()
    return sv.QMessageBox.StandardButton.Yes
sv.QMessageBox.question = staticmethod(_fake_yes)

w = fresh_window()
w._switch_page(PAGE_SSH); pump()
ssh = w._ssh_view
ssh._reload_sessions()
pump()

initial_count = ssh._sessions_list.count()
assert initial_count == 5, f'expected 5 sessions, got {initial_count}'

# Delete the currently-selected session 5 times. Each call must:
#  (a) not crash
#  (b) actually remove an item
#  (c) leave the remaining list consistent
for i in range(5):
    ssh._sessions_list.setCurrentRow(0)
    pump(2)
    ssh._on_delete_session()
    # Drain the QTimer.singleShot(0, ...) that does the actual
    # delete + reload in the real production path.
    pump(5)
    # Force a GC pass — if any dangling QListWidgetItem wrapper
    # survives, cleaning it up here is where Windows/PyQt6 segfaults.
    gc.collect()
    pump(2)

final_count = ssh._sessions_list.count()
assert final_count == 0, f'expected 0 sessions after 5 deletes, got {final_count}'

# Also exercise the No/cancel path — add one more session then
# cancel the delete to make sure cancel is still clean.
settings.save_ssh_host({
    'name': 'harness-cancel',
    'host': '10.20.30.99', 'port': 22, 'user': 'tester', 'key_path': '',
    'auth_method': 'password', 'save_credentials': False,
    'favorite': False, 'last_connected': '',
})
sv.QMessageBox.question = staticmethod(
    lambda *a, **k: sv.QMessageBox.StandardButton.No
)
ssh._reload_sessions(); pump()
assert ssh._sessions_list.count() == 1
ssh._sessions_list.setCurrentRow(0)
pump(2)
ssh._on_delete_session()
pump(5)
gc.collect()
assert ssh._sessions_list.count() == 1, 'cancel must not delete'

# Clean up the leftover session so re-runs start fresh.
settings.set_value('ssh_hosts', [])

w.close()
pump(20)
done()
"""


SCENARIOS["scanner_rapid_input"] = """
# Regression: the real user reported random crashes in the scanner
# on rapid input. Feed a mix of streaming results + rapid filter
# typing + rapid row clicks + rapid drawer toggles.
from scanner.host_scanner import HostInfo

w = fresh_window()
w._switch_page(PAGE_SCANNER); pump()

sv = w._scanner_view
table = sv._table

# Preload 50 hosts into the table.
for i in range(50):
    h = HostInfo(
        ip=f'10.0.0.{i}',
        status='alive' if i % 2 == 0 else 'dead',
        hostname=f'host{i}.local',
        mac=f'aa:bb:cc:dd:ee:{i:02x}',
        vendor='TestVendor',
        latency_ms=float(i % 10),
        ttl=64,
        open_ports=[22, 80] if i % 3 == 0 else [],
    )
    table.upsert_host(h)
pump()

# Rapid filter typing — each keystroke kicks the debounce timer.
for ch in 'host5 host1 10.0.0 tes':
    sv._toolbar._search.setText(sv._toolbar._search.text() + ch)
    pump(1)
# Let the debounce settle.
pump(10)

# Rapid row click / selection toggling.
proxy_model = table.model()
for i in range(20):
    cnt = proxy_model.rowCount()
    if cnt == 0:
        break
    row = i % min(cnt, 20)
    idx = proxy_model.index(row, 0)
    if idx.isValid():
        table._on_clicked(idx)
        pump(1)

# Rapid drawer close/re-open via clearing selection.
sv._on_drawer_closed()
pump(1)
sv._on_drawer_closed()
pump(1)

# Rapid status filter changes.
for status in ('alive', 'dead', 'all', 'alive', 'all'):
    table.set_status_filter(status)
    pump(1)

# Rapid filter clear.
sv._toolbar._search.setText('')
pump(20)

# Stream new results while user is interacting.
for i in range(50, 80):
    h = HostInfo(ip=f'10.0.0.{i}', status='alive', hostname=f'late{i}')
    table.upsert_host(h)
    if i % 5 == 0:
        table.set_filter(f'10.0.0.{i}')
        pump(1)

pump(10)
w.close()
pump(20)
done()
"""


SCENARIOS["ssh_rapid_tab_close_during_connect"] = """
w = fresh_window()
w._switch_page(PAGE_SSH); pump()
ssh = w._ssh_view
for i in range(5):
    ssh._open_session_tab(SSHProfile(
        name=f'r{i}', host='127.0.0.1', port=1, user='u', password='p'
    ))
    pump(1)
# Close all tabs in rapid succession while they are still connecting.
while ssh._tabs.count() > 0:
    ssh._on_tab_close_requested(0)
    pump(1)
w.close()
pump(20)
drain_ssh()
done()
"""


# ── Runner ───────────────────────────────────────────────────────────────────

def run_scenario(name: str, body: str, timeout: int = 60) -> tuple[str, bool, str]:
    script = PREAMBLE + "\n" + body
    try:
        r = subprocess.run(
            [HARNESS_PY, "-u", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        return (name, False, "timeout")
    if r.returncode == 0:
        return (name, True, "")
    # Non-zero exit → include any stderr output in the failure message.
    err = (r.stderr or "").strip().splitlines()
    tail = " | ".join(err[-3:]) if err else f"exit={r.returncode}"
    return (name, False, f"exit={r.returncode}  {tail}")


def main() -> int:
    results: list[tuple[str, bool, str]] = []
    for name, body in SCENARIOS.items():
        print(f"[RUN] {name}", flush=True)
        start = time.monotonic()
        result = run_scenario(name, body)
        dur = time.monotonic() - start
        _, ok, msg = result
        tag = "PASS" if ok else "FAIL"
        line = f"[{tag}] {name}  ({dur:.1f}s)"
        if msg:
            line += f"  — {msg}"
        print(line, flush=True)
        results.append(result)

    print("\n" + "=" * 60)
    print("CRASH HARNESS SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
