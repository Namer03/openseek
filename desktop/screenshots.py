import sys, time
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from openseek_desktop import theme
from openseek_desktop.core import Config
from openseek_desktop.ui import MainWindow
out = sys.argv[1]
app = QApplication([])
app.setStyleSheet(theme.QSS)
w = MainWindow(Config.load()); w.cfg.tray = False; w.resize(1360, 860); w.show()
def spin(ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents(); time.sleep(0.02)
spin(7000)
shots = [("dashboard", 0), ("library", 1), ("activity", 2), ("logs", 3), ("sharing", 4), ("settings", 5)]
for name, i in shots:
    w.go(i); spin(900)
    w.grab().save(f"{out}/{name}.png")
# detail of the first card
cards = w.cards
print("cards:", [(c.kind, c.title, c.tiles, c.complete) for c in cards])
if cards:
    w.open_card(cards[0]); spin(1500); w.grab().save(f"{out}/detail.png")
w.quit_app()
