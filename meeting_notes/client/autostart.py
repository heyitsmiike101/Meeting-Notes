r"""Start Meeting Notes when the person signs in to Windows (for the GPU helper).

A per-user registry value, ``HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MeetingNotes`` =
``"<path to the running exe>" --tray``: no administrator rights, removed by the uninstaller and when the helper is
turned off. Only the packaged exe registers itself; a source run, a test or another OS does nothing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("meeting_notes.client.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MeetingNotes"
TRAY_FLAG = "--tray"


def _registry() -> Optional[Any]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        return winreg
    except ImportError:  # pragma: no cover
        return None


def is_packaged() -> bool:
    """Whether this is the packaged (Nuitka / frozen) exe, the same test ``paths.app_folders`` uses."""
    main = sys.modules.get("__main__")
    return bool(
        getattr(sys, "frozen", False) or "__compiled__" in globals() or (main is not None and hasattr(main, "__compiled__"))
    )


def command_line(exe: Optional[str] = None) -> str:
    return f'"{Path(exe or sys.executable).resolve()}" {TRAY_FLAG}'


def is_set(registry: Optional[Any] = None) -> bool:
    reg = registry if registry is not None else _registry()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            reg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(registry: Optional[Any] = None, *, packaged: Optional[bool] = None, exe: Optional[str] = None) -> bool:
    """Write the Run value. Returns whether it was written (False off Windows or when not the packaged exe)."""
    reg = registry if registry is not None else _registry()
    if reg is None or not (is_packaged() if packaged is None else packaged):
        return False
    target = Path(exe or sys.executable).name.lower()
    if target.startswith(("python", "pythonw", "py.exe")):
        # Belt and braces: never make Windows start an interpreter at sign-in (a dev run or a test).
        log.info("not setting start at sign-in for %s", target)
        return False
    try:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ, command_line(exe))
        return True
    except OSError:
        log.warning("could not set start at sign-in", exc_info=True)
        return False


def remove(registry: Optional[Any] = None) -> bool:
    """Delete the Run value (fine when it is not there). Returns whether it is gone."""
    reg = registry if registry is not None else _registry()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True
    except OSError:
        log.warning("could not remove start at sign-in", exc_info=True)
        return False
    return True


def apply(enabled: bool, start_at_login: bool, registry: Optional[Any] = None, **kwargs) -> None:
    """Make the registry match the settings: set only while the helper is on and Start at login is on."""
    if registry is None and not is_packaged() and "packaged" not in kwargs:
        return  # a source run or a test never touches the real Run key
    if enabled and start_at_login:
        set_enabled(registry, **kwargs)
    else:
        remove(registry)
