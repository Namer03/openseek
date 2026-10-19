import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from . import theme
from .core import Config
from .ui import MainWindow, make_icon


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName("OpenSeek")
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(QIcon(make_icon()))
    f = QFont("Segoe UI Variable Text", 10)
    app.setFont(f)
    win = MainWindow(Config.load())
    if "--minimized" in argv and win.tray:
        pass
    else:
        win.show()
    if "--selftest" in argv:               # packaging check: render every page to PNGs, then quit
        import time
        out = argv[argv.index("--selftest") + 1]
        def spin(ms):
            end = time.time() + ms / 1000
            while time.time() < end:
                app.processEvents(); time.sleep(0.02)
        win.cfg.tray = False
        spin(6000)
        for i, name in enumerate(("dashboard", "library", "activity", "logs", "sharing", "settings")):
            win.go(i); spin(700); win.grab().save(f"{out}/{name}.png")
        win.quit_app()
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
