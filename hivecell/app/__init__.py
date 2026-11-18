"""Hivecell desktop app: a native window (Edge WebView2 on Windows), no server and no network port."""
import sys
from pathlib import Path

from .api import Api

UI = Path(__file__).with_name("ui.html")
ICON = Path(__file__).with_name("icon.ico")  # regenerate with scripts/make_icon.py

# Windows COLORREF is 0x00BBGGRR
INK = 0x000F1517  # #17150f
CREAM = 0x00E2EDF3  # #f3ede2
DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_BORDER_COLOR, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 20, 34, 35, 36


def _windows_identity():
    """Own taskbar identity, so Windows shows our icon instead of grouping us with Python."""
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Hivecell.App")


def _style_window(window):
    """Native frame kept (snap, resize, buttons); only its colours and icon change. Cosmetic: never fatal."""
    try:
        import ctypes

        from System import Action
        from System.Drawing import Icon

        form = window.native
        hwnd = ctypes.c_void_p(form.Handle.ToInt64())
        for attr, value in ((DWMWA_USE_IMMERSIVE_DARK_MODE, 1), (DWMWA_CAPTION_COLOR, INK),
                            (DWMWA_TEXT_COLOR, CREAM), (DWMWA_BORDER_COLOR, INK)):
            v = ctypes.c_int(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        icon = Icon(str(ICON))
        form.Invoke(Action(lambda: setattr(form, "Icon", icon)))  # WinForms objects belong to the UI thread
    except Exception:
        pass


def main():
    import webview

    if sys.platform == "win32":
        _windows_identity()
    api = Api()
    window = webview.create_window("Hivecell", html=UI.read_text(encoding="utf-8"), js_api=api,
                                   width=1320, height=860, min_size=(980, 620), background_color="#f3ede2")
    api._window = window
    if sys.platform == "win32":
        window.events.shown += lambda: _style_window(window)
    webview.start(icon=str(ICON))


__all__ = ["Api", "main"]
