import sys

from pdf_translate import main


def _hide_own_console() -> None:
    """Hide the console window when the exe was double-clicked, but keep it when run from a terminal."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleProcessList((ctypes.c_uint * 2)(), 2) <= 1:
        ctypes.windll.user32.ShowWindow(kernel32.GetConsoleWindow(), 0)


if __name__ == "__main__":
    if sys.platform == "win32" and len(sys.argv) == 1:
        _hide_own_console()
    main()
