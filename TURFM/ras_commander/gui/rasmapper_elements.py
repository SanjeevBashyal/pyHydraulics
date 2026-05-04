"""
RASMapper specific GUI element finders and actors.

Knows about the .NET WinForms RASMapper window structure:
TreeView layer navigation, context menus, toolbar, status bar.

Uses Win32Primitives for all low-level operations.

All methods are static and use the @log_call decorator.
"""

import time
from typing import Optional, Tuple

# Win32 imports - Windows only
try:
    import win32gui
    import win32con
    import win32api
    WIN32_AVAILABLE = True
except ImportError:
    win32gui = win32con = win32api = None
    WIN32_AVAILABLE = False

from ..LoggingConfig import get_logger
from ..Decorators import log_call
from .win32_primitives import Win32Primitives

logger = get_logger(__name__)


class RasMapperElements:
    """
    RASMapper specific GUI element finders and actors.

    RASMapper is a .NET WinForms application embedded within HEC-RAS.
    It has completely different window class names and control hierarchies
    from the VB6 main application.

    Key controls:
    - TreeView for layer navigation (Geometries, Terrain, Results, etc.)
    - Context menus for layer operations (right-click actions)
    - Toolbar for edit mode tools
    - Status bar for progress and status messages

    All methods are static and decorated with @log_call.
    """

    @staticmethod
    @log_call
    def find_rasmapper_window() -> Optional[Tuple[int, str]]:
        """
        Find the RASMapper window by title.

        Returns:
            (hwnd, title) tuple if found, None otherwise.
        """
        def callback(hwnd, windows):
            if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if "RAS Mapper" in title:
                    windows.append((hwnd, title))
            return True

        windows = []
        win32gui.EnumWindows(callback, windows)
        return windows[0] if windows else None

    @staticmethod
    @log_call
    def wait_for_rasmapper(timeout: int = 300, check_interval: int = 3) -> Optional[Tuple[int, str]]:
        """
        Wait for RASMapper window to appear and become responsive.

        Large projects may take several minutes to load geometry/terrain.
        This method checks window responsiveness, not just visibility.

        Args:
            timeout: Maximum seconds to wait. Default 300 (5 min).
            check_interval: Seconds between checks. Default 3.

        Returns:
            (hwnd, title) tuple if found and responsive, None on timeout.
        """
        start_time = time.time()
        last_log_time = start_time

        while time.time() - start_time < timeout:
            result = RasMapperElements.find_rasmapper_window()
            if result:
                hwnd, title = result
                if Win32Primitives.is_window_responsive(hwnd):
                    elapsed = int(time.time() - start_time)
                    logger.info(f"RASMapper opened: {title} (took {elapsed}s)")
                    return result
                else:
                    logger.debug("RASMapper window found but still loading...")

            elapsed = time.time() - start_time
            if elapsed - (last_log_time - start_time) >= 15:
                logger.info(f"Still waiting for RASMapper... ({int(elapsed)}s elapsed)")
                last_log_time = time.time()

            time.sleep(check_interval)

        elapsed = int(time.time() - start_time)
        logger.error(f"RASMapper window did not appear after {elapsed} seconds")
        return None

    @staticmethod
    @log_call
    def wait_for_rasmapper_idle(hwnd: int, timeout: int = 600, check_interval: int = 3) -> bool:
        """
        Wait for RASMapper to become idle after an operation.

        Checks window responsiveness as a proxy for operation completion.
        For mesh generation, also monitors the geometry HDF file modification time.

        Args:
            hwnd: RASMapper window handle.
            timeout: Maximum seconds to wait. Default 600 (10 min).
            check_interval: Seconds between checks. Default 3.

        Returns:
            True if RASMapper became idle within timeout.
        """
        start_time = time.time()
        last_log_time = start_time

        # Wait for window to become unresponsive (operation started)
        # then responsive again (operation completed)
        was_busy = False

        while time.time() - start_time < timeout:
            error_dialog = RasMapperElements.dismiss_rasmapper_error_dialog()
            if error_dialog:
                logger.error(
                    "RASMapper reported an error while regenerating mesh: "
                    f"{error_dialog}"
                )
                return False

            responsive = Win32Primitives.is_window_responsive(hwnd)

            if not responsive:
                was_busy = True
                logger.debug("RASMapper is busy...")
            elif was_busy and responsive:
                elapsed = int(time.time() - start_time)
                logger.info(f"RASMapper operation completed ({elapsed}s)")
                return True
            elif responsive and not was_busy:
                # Give it a moment — the operation may not have started yet
                time.sleep(1)

            elapsed = time.time() - start_time
            if elapsed - (last_log_time - start_time) >= 15:
                logger.info(f"Waiting for RASMapper operation... ({int(elapsed)}s elapsed)")
                last_log_time = time.time()

            time.sleep(check_interval)

        elapsed = int(time.time() - start_time)
        logger.warning(f"RASMapper operation did not complete after {elapsed} seconds")
        return False

    @staticmethod
    @log_call
    def find_rasmapper_error_dialog() -> Optional[Tuple[int, str]]:
        """
        Find RAS Mapper/HEC-RAS error dialogs that block mesh regeneration.

        HEC-RAS can show messages such as
        "Unknown Error [Failed to load Start Editing to Recompute Mesh]".
        Those dialogs keep the RAS Mapper window responsive, so a plain
        idle wait otherwise sits until the full timeout expires.
        """
        if not WIN32_AVAILABLE:
            return None

        matches = []

        def child_callback(child_hwnd, texts):
            try:
                text = win32gui.GetWindowText(child_hwnd)
                if text:
                    texts.append(text)
            except Exception:
                pass
            return True

        def callback(dialog_hwnd, found):
            try:
                if not (
                    win32gui.IsWindowVisible(dialog_hwnd)
                    and win32gui.IsWindowEnabled(dialog_hwnd)
                ):
                    return True

                title = win32gui.GetWindowText(dialog_hwnd)
                class_name = win32gui.GetClassName(dialog_hwnd)
                child_texts = []
                win32gui.EnumChildWindows(dialog_hwnd, child_callback, child_texts)
                combined = " ".join([title, *child_texts]).strip()
                text = combined.lower()
                is_dialog = class_name == "#32770"
                mentions_mapper = "ras mapper" in text or "hec-ras" in text
                has_mesh_error = (
                    "unknown error" in text
                    or "failed to load" in text
                    or "start editing" in text
                    or "recompute mesh" in text
                )
                if (is_dialog or mentions_mapper) and has_mesh_error:
                    found.append((dialog_hwnd, combined))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(callback, matches)
        return matches[0] if matches else None

    @staticmethod
    @log_call
    def dismiss_rasmapper_error_dialog() -> Optional[str]:
        result = RasMapperElements.find_rasmapper_error_dialog()
        if not result:
            return None

        dialog_hwnd, text = result
        for button_text in ("OK", "&OK", "Close", "&Close"):
            button = Win32Primitives.find_button_by_text(dialog_hwnd, button_text)
            if button:
                Win32Primitives.click_button(button)
                break
        return text
