import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from .credentials import Vault
from .db import Database, restore_database
from .ui.main import Window


def main():
    parser = argparse.ArgumentParser(description="Creator Control Center 2.0")
    default = Path(__file__).resolve().parent.parent / "data"
    parser.add_argument("--data-dir", type=Path, default=default)
    parser.add_argument("--restore", type=Path, help="Restore a backup into a new data directory")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    try:
        root = args.data_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(root / "ccc.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise ValueError("Эта рабочая база уже открыта или папка недоступна для записи.")
        database_path = root / "creator.sqlite3"
        if args.restore:
            restore_database(args.restore, database_path)
        db = Database(database_path)
        handler = RotatingFileHandler(root / "activity.log", maxBytes=1_000_000, backupCount=2,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("creator_control")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        workspace = db.setting("workspace_id")
        if not workspace:
            workspace = uuid4().hex
            db.settings({"workspace_id": workspace})
        window = Window(db, Vault(workspace))
        window.show()
        return app.exec()
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, "Creator Control Center", str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
