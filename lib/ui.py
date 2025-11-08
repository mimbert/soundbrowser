import signal, sys, enum
from PySide2 import QtCore, QtWidgets
from lib.soundbrowser_ui import SoundBrowserUI

def start_ui(startup_path):
    app = QtWidgets.QApplication([])
    app.setStyle("Fusion")
    app.default_palette = app.palette()
    sb = SoundBrowserUI(startup_path, app)
    def signal_handler(sig, frame):
        sb.clean_close()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    sb.show()
    signal_handler_timer = QtCore.QTimer()
    signal_handler_timer.start(250)
    signal_handler_timer.timeout.connect(lambda: None)
    sys.exit(app.exec_())
