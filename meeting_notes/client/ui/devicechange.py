"""Windows tells top-level windows when audio hardware comes or goes.

``WM_DEVICECHANGE`` is broadcast to every top-level window (no registration is
needed for ``DBT_DEVNODES_CHANGED``), so a Qt native event filter can use it as a
hint to re-scan for audio devices immediately instead of waiting for the next
poll. It is only a hint: the periodic poll in ``DeviceWatcher`` is what actually
guarantees a device is noticed, so anything going wrong here is harmless.
"""

from __future__ import annotations

import sys
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter

WM_DEVICECHANGE = 0x0219
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
_INTERESTING = {DBT_DEVNODES_CHANGED, DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE}


def is_device_change(message: int, wparam: int) -> bool:
    return message == WM_DEVICECHANGE and (int(wparam) & 0xFFFFFFFF) in _INTERESTING


class DeviceChangeFilter(QAbstractNativeEventFilter):
    def __init__(self, on_change: Callable[[], None]):
        super().__init__()
        self._on_change = on_change

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt naming
        try:
            if sys.platform == "win32" and bytes(event_type) == b"windows_generic_MSG":
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if is_device_change(msg.message, msg.wParam):
                    self._on_change()
        except Exception:  # noqa: BLE001 - a hint only; never disturb the event loop
            pass
        return False, 0


def install(app, on_change: Callable[[], None]):
    """Install the filter on Windows; returns it (the caller must keep it alive)."""
    if sys.platform != "win32" or app is None:
        return None
    flt = DeviceChangeFilter(on_change)
    app.installNativeEventFilter(flt)
    return flt
