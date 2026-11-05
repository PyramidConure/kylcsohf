import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from creator_control.db import Database
from creator_control.tools import sha256
from creator_control.ui.main import Window
from tests.test_core import MemoryVault, account_data


class UiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")

    def test_pages_and_calendar_flow(self):
        app = self.app
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as path:
            db = Database(Path(path) / "test.sqlite3")
            window = Window(db, MemoryVault())
            self.addCleanup(window.close)
            errors = []
            window.error = errors.append
            window.show()
            overview = window.stack.currentWidget()
            self.assertEqual(overview.next_title.text(), "Добавьте аккаунт")
            overview.next_action.click()
            self.assertEqual(window.stack.currentIndex(), 1)
            for index in range(9):
                window.navigation.setCurrentRow(index)
                app.processEvents()
                self.assertEqual(window.stack.currentIndex(), index)
            window.navigation.setCurrentRow(1)
            accounts = window.stack.currentWidget()
            accounts.form.set({"name": "Тестовый блог", "topic": "Наука"})
            accounts.save()
            self.assertEqual(accounts.table.rowCount(), 1)
            window.navigation.setCurrentRow(2)
            warmup = window.stack.currentWidget()
            self.assertEqual(warmup.table.rowCount(), 22)
            warmup.phase_buttons["research"][0].click()
            self.assertEqual(warmup.table.rowCount(), 5)
            warmup.all_phases.click()
            self.assertEqual(warmup.table.rowCount(), 22)
            warmup.table.selectRow(0)
            warmup.toggle()
            self.assertEqual(warmup.progress.value(), 5)
            self.assertEqual(warmup.status.text(), "Настройка профиля")
            self.assertEqual(warmup.toggle_button.text(), "Вернуть в задачи")
            if path := os.environ.get("CCC_QA_SCREENSHOT"):
                app.processEvents()
                self.assertTrue(window.grab().save(path))
            window.navigation.setCurrentRow(7)
            tasks = window.stack.currentWidget()
            tasks.form.set({"title": "Снять выпуск"})
            tasks.add()
            self.assertEqual(tasks.table.rowCount(), 23)
            window.navigation.setCurrentRow(0)
            self.assertEqual(window.stack.currentWidget().cards[0].text(), "1")
            db.settings({"theme": "light"})
            window.theme()
            app.processEvents()
            self.assertIn("#eff3f9", app.styleSheet())
            self.assertEqual(errors, [])
            window.close()

    def test_publication_filter_and_safe_local_check(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as path:
            root = Path(path)
            db = Database(root / "test.sqlite3")
            account = db.save_account(account_data("YouTube"))
            instagram = db.save_account(account_data("Instagram"))
            file = root / "Тест.mp4"
            file.write_bytes(b"mock video")
            video = db.add_video(file, sha256(file), {"width": 1080, "height": 1920, "seconds": 4})
            another = root / "Второй ролик.mp4"
            another.write_bytes(b"second mock video")
            second = db.add_video(another, sha256(another), {"width": 1080, "height": 1920, "seconds": 5})
            window = Window(db, MemoryVault())
            self.addCleanup(window.close)
            errors = []
            window.error = errors.append
            window.show()
            window.new_post(video)
            posts = window.stack.currentWidget()
            self.assertEqual(posts.form.values()["video_id"], video)
            posts.form.set({"account_id": instagram, "caption": "Моё описание"})
            self.assertFalse(posts.form.widget("video_url").isHidden())
            self.assertTrue(posts.form.widget("privacy").isHidden())
            posts.form.set({"video_id": second})
            self.assertEqual(posts.form.values()["title"], "Второй ролик")
            self.assertEqual(posts.form.values()["caption"], "Моё описание")
            tomorrow = date.today() + timedelta(days=1)
            posts.day_only.setChecked(True)
            posts.form.set({"account_id": account, "due": f"{tomorrow.isoformat()} 09:00"})
            self.assertTrue(posts.form.widget("video_url").isHidden())
            self.assertFalse(posts.form.widget("privacy").isHidden())
            posts.save_button.click()
            identifier = posts.table.selected()
            self.assertIsNotNone(identifier)
            self.assertEqual(posts.calendar.selectedDate().toPython(), tomorrow)
            self.assertTrue(posts.send_button.isEnabled())
            posts.form.set({"title": "Несохранённая версия"})
            with self.assertRaisesRegex(ValueError, "Сохраните"):
                posts.send(False)
            posts.save_button.click()
            with patch.object(posts.publisher, "client") as client:
                posts.send_button.click()
                for _ in range(100):
                    if window.worker is None:
                        break
                    QTest.qWait(20)
                self.assertIsNone(window.worker)
                client.assert_not_called()
            self.assertIn("Тест пройден", window.statusBar().currentMessage())
            self.assertEqual(db.post(identifier)["state"], "planned")
            posts.calendar.setSelectedDate(QDate.currentDate())
            self.assertEqual(posts.table.rowCount(), 0)
            self.assertIsNone(posts.editing)
            self.assertFalse(posts.send_button.isEnabled())
            self.assertFalse(posts.check_button.isEnabled())
            with self.assertRaises(ValueError):
                posts.send(False)
            self.assertEqual(errors, [])
            window.close()
