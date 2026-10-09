"""Send prints through Bambu Connect (Bambu's official, authorized sender) by driving its window with
Windows UI Automation: named buttons, no screenshots, no computer use.

Connect must run with Chromium's --force-renderer-accessibility switch, or its page isn't exposed.
Bambu Connect is ONE shared window: every send holds a lock file, and nothing is pressed unless the
dialog shows our file and our printer (a send once went out with another session's file loaded).
"""
import json
import os
import subprocess
import time
import urllib.parse
from pathlib import Path

import uiautomation as auto

EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "bambu-connect" / "Bambu Connect.exe"
LOCK = Path(__file__).parent / ".connect.lock"
FLAG = "--force-renderer-accessibility"
INTERACTIVE = ("ButtonControl", "TextControl", "HyperlinkControl", "ListItemControl", "ComboBoxControl",
               "CheckBoxControl", "RadioButtonControl", "EditControl", "MenuItemControl", "ImageControl")


class SendAborted(RuntimeError):
    pass


_user32 = __import__("ctypes").windll.user32


def _hwnd() -> int:
    """Connect's top-level window handle, found even when hidden (the UIA root skips hidden windows)."""
    return _user32.FindWindowW("Chrome_WidgetWin_1", "Bambu Connect (Beta)") or         _user32.FindWindowW("Chrome_WidgetWin_1", "Bambu Connect")


def window():
    h = _hwnd()
    return auto.ControlFromHandle(h) if h else None


def document(wait: float = 5):
    w = window()
    if not w:
        return None
    doc = w.DocumentControl(searchDepth=20)
    return doc if doc.Exists(wait) else None


def controls(root=None, depth: int = 45):
    root = root or document()
    return [c for c, _ in auto.WalkControl(root, maxDepth=depth)] if root else []


def dump(max_items: int = 150) -> list[str]:
    doc = document()
    if not doc:
        return ["<no Bambu Connect document (is it running with --force-renderer-accessibility?)>"]
    out = []
    for c, d in auto.WalkControl(doc, maxDepth=45):
        if c.ControlTypeName in INTERACTIVE and (c.Name or "").strip():
            r = c.BoundingRectangle
            out.append(f"{'  ' * min(d, 12)}{c.ControlTypeName[:-7]}: {c.Name[:80]!r} @({r.left},{r.top})")
            if len(out) >= max_items:
                break
    return out


def _running_flags() -> list[str]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Process -Filter \"Name='Bambu Connect.exe'\").CommandLine"],
                         capture_output=True, text=True, timeout=30).stdout
    return [l for l in out.splitlines() if l.strip()]


def _win_state(w) -> dict:
    h = w.NativeWindowHandle
    return {"visible": bool(auto.IsWindowVisible(h)), "minimized": bool(auto.IsIconic(h))}


def make_readable(timeout: float = 15) -> dict:
    """Get Connect's page exposed to UI Automation whatever state its window is in.
    Measured behaviour (Connect 2.5 beta): the page tree is built only when the window is ACTIVATED,
    survives losing focus or being covered, and is thrown away on minimize/hide. So: restore without
    focus, and if the page still isn't there, activate Connect for a moment and hand focus straight
    back. Returns the original state so the caller can put it back."""
    h = _hwnd()
    if not h:
        raise SendAborted("Bambu Connect window not found")
    before = {"visible": bool(_user32.IsWindowVisible(h)), "minimized": bool(_user32.IsIconic(h))}
    if before["minimized"]:
        _user32.ShowWindow(h, 4)   # SW_SHOWNOACTIVATE
    elif not before["visible"]:
        _user32.ShowWindow(h, 8)   # SW_SHOWNA
    if document(2):
        return before
    prev = _user32.GetForegroundWindow()
    try:
        window().SetActive()       # uiautomation's SetActive handles Windows' foreground-lock rules
        end = time.time() + timeout
        while time.time() < end:
            if document(1):
                return before
            time.sleep(0.5)
    finally:
        if prev and prev != h:
            _user32.SetForegroundWindow(prev)
    raise SendAborted(f"Bambu Connect's page isn't exposed even after focusing it (window was {before})")


def restore_state(before: dict | None) -> None:
    h = _hwnd()
    if not h or not before:
        return
    if not before["visible"]:
        _user32.ShowWindow(h, 0)  # SW_HIDE (was hidden, e.g. closed to tray)
    elif before["minimized"]:
        _user32.ShowWindow(h, 7)  # SW_SHOWMINNOACTIVE


def ensure_running() -> dict:
    """Start Connect with the accessibility switch if needed, then make its page readable. Never
    restarts a running Connect: another session (or the user) may be mid-send in it."""
    lines = _running_flags()
    if not lines:
        subprocess.Popen([str(EXE), FLAG])
        for _ in range(40):
            if window():
                break
            time.sleep(1)
    elif not any(FLAG in l for l in lines):
        # Started without the switch (e.g. an old shortcut). Restart it with the switch; worst case an
        # unsent dialog is lost, nothing on a printer is affected. We hold the Connect lock here.
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-Process 'Bambu Connect' -ErrorAction SilentlyContinue | "
                        "ForEach-Object { $_.CloseMainWindow() | Out-Null }; Start-Sleep 3; "
                        "Get-Process 'Bambu Connect' -ErrorAction SilentlyContinue | Stop-Process -Force"],
                       capture_output=True, timeout=60)
        subprocess.Popen([str(EXE), FLAG])
        for _ in range(40):
            if window():
                break
            time.sleep(1)
        time.sleep(3)  # let the app finish loading its page
    return make_readable()


def status() -> dict:
    """Read-only diagnostics for Connect automation."""
    with auto.UIAutomationInitializerInThread():
        lines = _running_flags()
        w = window()
        info = {"running": bool(lines), "accessibility_flag": any(FLAG in l for l in lines),
                "window": {"visible": bool(_user32.IsWindowVisible(_hwnd())), "minimized": bool(_user32.IsIconic(_hwnd()))} if w else None, "page_readable_now": bool(document(2))}
        if w and info["accessibility_flag"]:
            try:
                before = make_readable(10)
                doc = document(2)
                info["readable_after_fix"] = bool(doc)
                info["sample"] = [c.Name for c in controls(doc, 25) if c.ControlTypeName in
                                  ("HyperlinkControl", "ButtonControl") and c.Name][:8]
                restore_state(before)
                h = _hwnd()
                info["window_after"] = {"visible": bool(_user32.IsWindowVisible(h)), "minimized": bool(_user32.IsIconic(h))}
            except SendAborted as e:
                info["readable_after_fix"] = False
                info["error"] = str(e)
        return info


def _find(pred, root=None, wait: float = 10):
    end = time.time() + wait
    while time.time() < end:
        hit = next((c for c in controls(root) if pred(c)), None)
        if hit:
            return hit
        time.sleep(0.5)
    return None


def _texts(root) -> list[str]:
    return [c.Name for c in controls(root, 30) if c.ControlTypeName == "TextControl" and c.Name]


def _press(c) -> None:
    # Works for any invokable control type (buttons, and the groups Connect uses for dropdowns).
    c.GetPattern(auto.PatternId.InvokePattern).Invoke()


def _dialog_of(anchor):
    """Climb from a control to the smallest ancestor containing a Send ('confirm') button."""
    node = anchor
    while node:
        if any(x.Name == "confirm" and x.ControlTypeName == "ButtonControl" for x in controls(node, 15)):
            return node
        node = node.GetParentControl()
    return None


def acquire_lock(owner: str, stale_s: int = 300) -> None:
    if LOCK.exists():
        info = json.loads(LOCK.read_text(encoding="utf-8") or "{}")
        if time.time() - info.get("t", 0) < stale_s:
            raise SendAborted(f"Bambu Connect is being used by another send ({info.get('owner')}); try again shortly")
    LOCK.write_text(json.dumps({"owner": owner, "t": time.time()}), encoding="utf-8")


def release_lock() -> None:
    LOCK.unlink(missing_ok=True)


SLOT_RE = __import__("re").compile(r"^(HT-[A-D]|[A-D][1-4]|Ext)$")


def send(file: Path, printer_label: str, dry_run: bool = False, expected_slots: list | None = None) -> dict:
    """Import `file` into Connect, open Send to print, verify file + printer, press Send.
    dry_run=True stops right before Send and cancels the dialog."""
    # MCP runs tools on worker threads; UI Automation (COM) must be initialised per thread.
    with auto.UIAutomationInitializerInThread():
        before = None
        stem = file.name.split(".")[0]
        acquire_lock(f"{stem} -> {printer_label}")
        try:
            before = ensure_running()
            return _send(file, printer_label, dry_run, expected_slots)
        finally:
            restore_state(before)  # put the window back how the user had it
            release_lock()


def _send(file: Path, printer_label: str, dry_run: bool, expected_slots: list | None) -> dict:
    stem = file.name.split(".")[0]
    try:
        os.startfile("bambu-connect://import-file?path=" + urllib.parse.quote(str(file), safe="")
                     + "&name=" + urllib.parse.quote(stem, safe="") + "&version=1.0.0")
        time.sleep(2)
        # Optional confirmation ("You will import the file ..."): accept only if it names OUR file.
        imp = _find(lambda c: c.ControlTypeName == "TextControl" and "You will import the file" in (c.Name or ""), wait=4)
        if imp:
            if f'"{stem}"' not in imp.Name:
                raise SendAborted(f"Import dialog is for a different file: {imp.Name!r}")
            # The green "Import Gcode 3MF" button's accessible name is "confirm" (the label is a child
            # text). Press the confirm button of THIS dialog only.
            dlg = imp.GetParentControl()
            while dlg and not (dlg.ControlTypeName == "CustomControl" and dlg.Name == "Import file"):
                dlg = dlg.GetParentControl()
            btn = next((c for c in controls(dlg, 10) if c.ControlTypeName == "ButtonControl" and c.Name == "confirm"),
                       None) if dlg else None
            if not btn:
                raise SendAborted("Import dialog's confirm button not found")
            _press(btn)
        # The plate page must show OUR file before anything else happens.
        page_file = _find(lambda c: c.ControlTypeName == "TextControl" and (c.Name or "") == f"{stem}.3mf", wait=15)
        if not page_file:
            raise SendAborted(f"Connect isn't showing {stem}.3mf after import")
        details = _texts(page_file.GetParentControl().GetParentControl().GetParentControl())
        print_btn = _find(lambda c: c.ControlTypeName == "ButtonControl" and c.Name == "Print", wait=5)
        if not print_btn:
            raise SendAborted("Print button not found on the plate page")
        _press(print_btn)
        title = _find(lambda c: c.ControlTypeName == "TextControl" and c.Name == "Send to print", wait=10)
        dlg = _dialog_of(title) if title else None
        if not dlg:
            raise SendAborted("Send to print dialog didn't open")
        # Re-check the file right before sending: the page behind the dialog must still be ours.
        if not _find(lambda c: c.ControlTypeName == "TextControl" and (c.Name or "") == f"{stem}.3mf", wait=2):
            _cancel(dlg)  # never leave a Send dialog open: anyone's click would send it
            raise SendAborted("The loaded file changed while sending; aborting")
        if _selected_printer(dlg) != printer_label:
            _select_printer(dlg, printer_label)
            dlg = _current_dialog()             # the dialog re-renders after a printer switch
            if not dlg:
                raise SendAborted("Send dialog disappeared after switching printer. Not sent.")
        dtexts = _texts(dlg)
        if _selected_printer(dlg) != printer_label:
            _cancel(dlg)
            raise SendAborted(f"Couldn't select printer {printer_label!r} (dialog shows {_selected_printer(dlg)!r}). Not sent.")
        # AMS mapping shown in the dialog (e.g. file PLA -> "A1") must be the slot preflight chose.
        shown = [x for x in dtexts if SLOT_RE.match(x)]
        if expected_slots and sorted(shown) != sorted(s for s in expected_slots if s):
            _cancel(dlg)
            raise SendAborted(f"Connect maps the filament to {shown}, preflight planned {expected_slots}. Not sent.")
        summary = {"file": f"{stem}.3mf", "printer": printer_label, "page": details, "dialog": dtexts,
                   "ams_slots": shown}
        if dry_run:
            _cancel(dlg)
            summary["sent"] = False
            return summary
        confirm = next(c for c in controls(dlg, 15) if c.ControlTypeName == "ButtonControl" and c.Name == "confirm")
        _press(confirm)
        time.sleep(2)
        summary["sent"] = True
        return summary
    finally:
        pass  # the lock is held and released by send()


def _printer_dropdown(dlg):
    """The printer picker: a group named '<printer> chevron_down'."""
    return next((c for c in controls(dlg, 20) if c.ControlTypeName == "GroupControl"
                 and (c.Name or "").endswith(" chevron_down")), None)


def _selected_printer(dlg) -> str | None:
    d = _printer_dropdown(dlg)
    return d.Name[: -len(" chevron_down")] if d else None


def _select_printer(dlg, label: str) -> None:
    d = _printer_dropdown(dlg)
    if not d:
        return
    _press(d)                                   # open the list (Invoke works for the picker itself)
    item = _find(lambda c: c.ControlTypeName == "MenuItemControl" and c.Name == label, wait=5)
    if not item:
        _press(d)                               # close it again
        return
    # The menu items ignore accessibility "press"; they react to mouse events. Post a click straight
    # to Connect's window: no real cursor movement, no focus needed.
    _post_click(item)
    end = time.time() + 8                       # wait for the dialog to switch (mapping reloads too)
    while time.time() < end:
        d = _current_dialog()
        if d and _selected_printer(d) == label:
            break
        time.sleep(0.5)
    time.sleep(1.5)


def _current_dialog():
    """The Send to print dialog as it is NOW (Connect re-renders it, e.g. after a printer switch)."""
    title = _find(lambda c: c.ControlTypeName == "TextControl" and c.Name == "Send to print", wait=3)
    return _dialog_of(title) if title else None


def _post_click(ctrl) -> None:
    import ctypes
    from ctypes import wintypes
    r = ctrl.BoundingRectangle
    h = _hwnd()
    pt = wintypes.POINT((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    _user32.ScreenToClient(h, ctypes.byref(pt))
    lp = (pt.y << 16) | (pt.x & 0xFFFF)
    for msg, wp in ((0x0200, 0), (0x0201, 1), (0x0202, 0)):  # WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP
        _user32.PostMessageW(h, msg, wp, lp)
        time.sleep(0.05)


def _cancel(dlg=None) -> None:
    """Cancel the Send dialog, looked up fresh; never leave it open (anyone's click would send)."""
    for _ in range(3):
        d = _current_dialog()
        if not d:
            return
        c = next((x for x in controls(d, 15) if x.ControlTypeName == "ButtonControl" and x.Name == "cancel"), None)
        if c:
            _press(c)
        time.sleep(1)
