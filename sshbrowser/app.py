from __future__ import annotations

import asyncio
import sys

import qasync
from PySide6.QtWidgets import QApplication

from .config import ProfileStore
from .main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SSH Browser")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    store = ProfileStore()
    win = MainWindow(store)
    win.show()
    if not store.profiles:
        win.manage_hosts()
    with loop:
        loop.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
