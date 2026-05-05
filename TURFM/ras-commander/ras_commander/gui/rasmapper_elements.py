"""
RASMapper specific GUI element finders and actors.

Knows about the .NET WinForms RASMapper window structure:
TreeView layer navigation, context menus, toolbar, status bar, and the
embedded editor windows used for 2D flow-area mesh generation.

Uses Win32Primitives for all low-level operations.

All methods are static and use the @log_call decorator.
"""

import time
from typing import List, Optional, Tuple

# Win32 imports - Windows only
try:
    import win32gui
    import win32con
    import win32api
    WIN32_AVAILABLE = True
except ImportError:
    win32gui = win32con = win32api = None
    WIN32_AVAILABLE = False

try:
    from pywinauto import Application, Desktop
    from pywinauto.keyboard import send_keys
    PYWINAUTO_AVAILABLE = True
except ImportError:
    Application = None
    Desktop = None
    send_keys = None
    PYWINAUTO_AVAILABLE = False

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
    def _normalize_menu_text(text: str) -> str:
        """Normalize menu labels for resilient text matching."""
        return (
            text.replace("&", "")
            .replace("...", "")
            .replace("\u2026", "")
            .strip()
            .lower()
        )

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
    def _iter_uia_matches(
        control_type: Optional[str] = None,
        title: Optional[str] = None,
        automation_id: Optional[str] = None,
        parent=None,
    ) -> List:
        """
        Enumerate UIA descendants inside the RAS Mapper window.

        pywinauto can expose duplicate wrappers for WinForms controls. This
        helper filters duplicates by handle and visible rectangle.
        """
        if not PYWINAUTO_AVAILABLE:
            return []

        container = parent or RasMapperElements.get_rasmapper_wrapper()
        if container is None:
            return []

        matches = []
        seen = set()

        for control in container.descendants():
            try:
                element = control.element_info
                if control_type and element.control_type != control_type:
                    continue
                if title is not None and control.window_text() != title:
                    continue
                if automation_id is not None:
                    element_id = getattr(element, "automation_id", "")
                    if element_id != automation_id:
                        continue

                rect = control.rectangle()
                handle = getattr(control, "handle", None)
                key = (
                    handle,
                    rect.left,
                    rect.top,
                    rect.right,
                    rect.bottom,
                    control.window_text(),
                    element.control_type,
                    getattr(element, "automation_id", ""),
                )
                if key in seen:
                    continue

                seen.add(key)
                matches.append(control)
            except Exception:
                continue

        return matches

    @staticmethod
    @log_call
    def get_rasmapper_wrapper():
        """
        Return the top-level RAS Mapper UIA wrapper, or None if unavailable.
        """
        if not PYWINAUTO_AVAILABLE:
            logger.warning("pywinauto is not available for UIA inspection")
            return None

        try:
            windows = Desktop(backend="uia").windows()
        except Exception as exc:
            logger.warning("Could not enumerate UIA windows: %s", exc)
            return None

        for window in windows:
            try:
                if window.window_text() == "RAS Mapper":
                    return window
            except Exception:
                continue
        return None

    @staticmethod
    @log_call
    def get_rasmapper_win32_wrapper():
        """
        Return the top-level RAS Mapper wrapper from the win32 backend.
        """
        if not PYWINAUTO_AVAILABLE:
            logger.warning("pywinauto is not available for win32 inspection")
            return None

        try:
            found = RasMapperElements.find_rasmapper_window()
            if found:
                hwnd, _ = found
                return Desktop(backend="win32").window(handle=hwnd)

            app = Application(backend="win32").connect(title="RAS Mapper")
            return app.window(title="RAS Mapper")
        except Exception as exc:
            logger.warning("Could not connect to RAS Mapper win32 wrapper: %s", exc)
            return None

    @staticmethod
    @log_call
    def get_treeview_wrapper():
        """
        Return the populated left-hand RAS Mapper TreeView wrapper.
        """
        window = RasMapperElements.get_rasmapper_win32_wrapper()
        if window is None:
            return None

        try:
            for child in window.children():
                if child.friendly_class_name() == "TreeView":
                    return child
        except Exception as exc:
            logger.warning("Could not enumerate RAS Mapper children: %s", exc)
            return None
        return None

    @staticmethod
    @log_call
    def select_tree_path(path: List[str]):
        """
        Select a node in the populated RAS Mapper tree by path.
        """
        tree = RasMapperElements.get_treeview_wrapper()
        if tree is None:
            return None

        try:
            item = tree.get_item(path)
            item.select()
            return item
        except Exception as exc:
            logger.warning("Could not select tree path %s: %s", path, exc)
            return None

    @staticmethod
    @log_call
    def open_tree_context_menu(path: List[str]) -> bool:
        """
        Open the context menu for a selected tree node using Shift+F10.
        """
        window = RasMapperElements.get_rasmapper_win32_wrapper()
        item = RasMapperElements.select_tree_path(path)
        if window is None or item is None or send_keys is None:
            return False

        try:
            window.set_focus()
            time.sleep(0.2)
            send_keys("+{F10}")
            time.sleep(0.8)
            return True
        except Exception as exc:
            logger.warning("Could not open context menu for %s: %s", path, exc)
            return False

    @staticmethod
    @log_call
    def find_embedded_window(
        title: str,
        automation_id: Optional[str] = None,
    ):
        """
        Find an embedded editor/layer-properties window inside RAS Mapper.
        """
        matches = RasMapperElements._iter_uia_matches(
            control_type="Window",
            title=title,
            automation_id=automation_id,
        )
        return matches[0] if matches else None

    @staticmethod
    @log_call
    def wait_for_embedded_window(
        title: str,
        automation_id: Optional[str] = None,
        timeout: int = 60,
        check_interval: float = 1.0,
    ):
        """
        Wait for an embedded editor/layer-properties window to appear.
        """
        start = time.time()
        while time.time() - start < timeout:
            window = RasMapperElements.find_embedded_window(
                title=title,
                automation_id=automation_id,
            )
            if window is not None:
                return window
            time.sleep(check_interval)
        return None

    @staticmethod
    @log_call
    def find_control(
        control_type: str,
        title: Optional[str] = None,
        automation_id: Optional[str] = None,
        parent=None,
    ):
        """
        Find a UIA control inside RAS Mapper by type and optional identifiers.
        """
        matches = RasMapperElements._iter_uia_matches(
            control_type=control_type,
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        return matches[0] if matches else None

    @staticmethod
    @log_call
    def get_mesh_status_text() -> str:
        """
        Read the mesh status document text from the 2D Flow Area editor.
        """
        editor = RasMapperElements.find_embedded_window(
            title="2D Flow Area Editor",
            automation_id="D2Editor",
        )
        if editor is None:
            return ""

        control = RasMapperElements.find_control(
            control_type="Document",
            automation_id="rtbMetadata",
            parent=editor,
        )
        if control is None:
            return ""

        try:
            return control.window_text()
        except Exception:
            return ""

    @staticmethod
    @log_call
    def get_status_list_items() -> List[str]:
        """
        Read visible status/log lines from the RAS Mapper shell.
        """
        main = RasMapperElements.get_rasmapper_wrapper()
        if main is None:
            return []

        items = []
        for control in RasMapperElements._iter_uia_matches(
            control_type="ListItem",
            parent=main,
        ):
            try:
                text = control.window_text()
                if text:
                    items.append(text)
            except Exception:
                continue
        return items

    @staticmethod
    @log_call
    def click_menu_item(title: str) -> bool:
        """
        Click a currently-visible context/menu item by its text.
        """
        target = RasMapperElements._normalize_menu_text(title)
        candidates = []

        for candidate in RasMapperElements._iter_uia_matches(
            control_type="MenuItem",
        ):
            try:
                candidate_text = candidate.window_text()
                normalized = RasMapperElements._normalize_menu_text(
                    candidate_text
                )
                if normalized == target or target in normalized:
                    rect = candidate.rectangle()
                    area = max(0, rect.right - rect.left) * max(
                        0, rect.bottom - rect.top
                    )
                    candidates.append((area, candidate))
            except Exception:
                continue

        candidates.sort(key=lambda item: item[0], reverse=True)
        menu_item = candidates[0][1] if candidates else None

        if menu_item is None:
            logger.warning("Could not find menu item: %s", title)
            return False

        try:
            menu_item.click_input()
            time.sleep(0.8)
            return True
        except Exception as exc:
            logger.warning("Could not click menu item '%s': %s", title, exc)
            return False

    @staticmethod
    @log_call
    def get_combobox_selected_text(
        title: str,
        automation_id: Optional[str] = None,
        parent=None,
    ) -> str:
        """Return the selected text for a WinForms combo box."""
        combo = RasMapperElements.find_control(
            control_type="ComboBox",
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if combo is None:
            return ""

        try:
            selected = combo.selected_text()
        except Exception:
            return ""

        if selected in {None, "(None)"}:
            return ""
        return str(selected).strip()

    @staticmethod
    @log_call
    def set_combobox_selection_via_keys(
        title: str,
        value: str,
        automation_id: Optional[str] = None,
        parent=None,
    ) -> bool:
        """
        Set a WinForms combo-box selection using keyboard input.

        Some RAS Mapper grids expose the combo control but not its dropdown
        items through UIA. In that case direct selection APIs fail, while
        focus + keyboard entry works reliably.
        """
        if send_keys is None:
            logger.warning("pywinauto keyboard support is not available")
            return False

        combo = RasMapperElements.find_control(
            control_type="ComboBox",
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if combo is None:
            logger.warning(
                "Could not find RAS Mapper combo box: title=%s automation_id=%s",
                title,
                automation_id,
            )
            return False

        current = RasMapperElements.get_combobox_selected_text(
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if current.lower() == value.lower():
            return True

        try:
            combo.click_input()
            time.sleep(0.5)
            send_keys(value)
            time.sleep(0.2)
            send_keys("{ENTER}")
            time.sleep(0.8)
        except Exception as exc:
            logger.warning(
                "Could not type combo selection '%s' into '%s': %s",
                value,
                title,
                exc,
            )
            return False

        selected = RasMapperElements.get_combobox_selected_text(
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if selected.lower() == value.lower():
            return True

        try:
            combo.click_input()
            time.sleep(0.5)
            send_keys("{DOWN}{ENTER}")
            time.sleep(0.8)
        except Exception as exc:
            logger.warning(
                "Could not advance combo selection for '%s': %s",
                title,
                exc,
            )
            return False

        selected = RasMapperElements.get_combobox_selected_text(
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        return selected.lower() == value.lower()

    @staticmethod
    @log_call
    def click_embedded_dialog_button(
        dialog_title: str,
        button_text: str,
        max_clicks: int = 4,
    ) -> int:
        """
        Click a button in embedded RAS Mapper child dialogs.

        Some dialogs appear twice in the UIA tree; this clicks through all
        visible matches up to max_clicks and returns the number clicked.
        """
        main = RasMapperElements.get_rasmapper_wrapper()
        if main is None:
            return 0

        clicks = 0
        for window in RasMapperElements._iter_uia_matches(
            control_type="Window",
            title=dialog_title,
            parent=main,
        ):
            try:
                for button in window.descendants(control_type="Button"):
                    if button.window_text() == button_text:
                        button.click_input()
                        clicks += 1
                        time.sleep(0.4)
                        break
                if clicks >= max_clicks:
                    break
            except Exception:
                continue
        return clicks

    @staticmethod
    @log_call
    def get_embedded_dialog_list_items(dialog_title: str) -> List[str]:
        """
        Read list-item text from embedded RAS Mapper child dialogs.
        """
        main = RasMapperElements.get_rasmapper_wrapper()
        if main is None:
            return []

        items = []
        seen = set()
        for window in RasMapperElements._iter_uia_matches(
            control_type="Window",
            title=dialog_title,
            parent=main,
        ):
            try:
                for list_item in window.descendants(control_type="ListItem"):
                    text = list_item.window_text()
                    if text and text not in seen:
                        seen.add(text)
                        items.append(text)
            except Exception:
                continue
        return items

    @staticmethod
    @log_call
    def post_button_click(
        title: Optional[str] = None,
        automation_id: Optional[str] = None,
        parent=None,
    ) -> bool:
        """
        Post a BM_CLICK to a WinForms button without blocking the caller.
        """
        if not WIN32_AVAILABLE:
            logger.warning("Win32 support is not available")
            return False

        button = RasMapperElements.find_control(
            control_type="Button",
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if button is None:
            logger.warning(
                "Could not find RAS Mapper button: title=%s automation_id=%s",
                title,
                automation_id,
            )
            return False

        handle = getattr(button, "handle", None)
        if not handle:
            logger.warning(
                "RAS Mapper button has no HWND: title=%s automation_id=%s",
                title,
                automation_id,
            )
            return False

        try:
            win32gui.PostMessage(handle, win32con.BM_CLICK, 0, 0)
            logger.info(
                "Posted BM_CLICK to button: title=%s automation_id=%s hwnd=%s",
                title,
                automation_id,
                handle,
            )
            return True
        except Exception as exc:
            logger.error("Could not click RAS Mapper button: %s", exc)
            return False

    @staticmethod
    @log_call
    def set_checkbox_state(
        title: str,
        checked: bool,
        automation_id: Optional[str] = None,
        parent=None,
    ) -> bool:
        """
        Set a WinForms checkbox to the requested state if its toggle pattern
        is available.
        """
        checkbox = RasMapperElements.find_control(
            control_type="CheckBox",
            title=title,
            automation_id=automation_id,
            parent=parent,
        )
        if checkbox is None:
            logger.warning(
                "Could not find RAS Mapper checkbox: title=%s automation_id=%s",
                title,
                automation_id,
            )
            return False

        try:
            current_state = bool(checkbox.get_toggle_state())
        except Exception as exc:
            logger.warning(
                "Could not read checkbox state for '%s': %s",
                title,
                exc,
            )
            return False

        if current_state == checked:
            return True

        handle = getattr(checkbox, "handle", None)
        if not handle:
            logger.warning("Checkbox '%s' has no HWND", title)
            return False

        try:
            win32gui.PostMessage(handle, win32con.BM_CLICK, 0, 0)
            time.sleep(0.5)
            return bool(checkbox.get_toggle_state()) == checked
        except Exception as exc:
            logger.error("Could not set checkbox '%s': %s", title, exc)
            return False
