import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from creator_control.db import Database
from creator_control.tools import sha256
from creator_control.ui.main import Window
from creator_control.ui.clock import Clock
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
            self.assertEqual([window.navigation.item(i).text() for i in (0, 1)], ["Обзор", "ИИ-чат"])
            overview = window.stack.currentWidget()
            self.assertEqual(overview.next_title.text(), "Добавьте аккаунт")
            overview.next_action.click()
            self.assertIs(window.stack.currentWidget(), window.page("accounts"))
            links = overview.findChildren(QPushButton, "metricLink")
            targets = ("accounts", "videos", "posts", "tasks", "warmup", "posts", "settings")
            self.assertEqual(len(links), len(targets))
            for link, target in zip(links, targets):
                window.navigate("overview")
                link.click()
                self.assertIs(window.stack.currentWidget(), window.page(target))
            for index in range(window.navigation.count()):
                window.navigation.setCurrentRow(index)
                app.processEvents()
                self.assertEqual(window.stack.currentIndex(), index)
            window.navigate("accounts")
            accounts = window.stack.currentWidget()
            accounts.form.set({"name": "Тестовый блог", "topic": "Наука"})
            accounts.save()
            self.assertEqual(accounts.table.rowCount(), 1)
            window.navigate("warmup")
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
            window.navigate("tasks")
            tasks = window.stack.currentWidget()
            tasks.form.set({"title": "Снять выпуск"})
            tasks.add()
            self.assertEqual(tasks.table.rowCount(), 23)
            window.navigate("overview")
            self.assertEqual(window.stack.currentWidget().cards[0].text(), "1")
            db.settings({"theme": "light"})
            window.theme()
            app.processEvents()
            self.assertIn("#eff3f9", app.styleSheet())
            self.assertEqual(errors, [])
            window.close()

    def test_clock_city_offsets_dates_and_selection_are_saved(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as path:
            db = Database(Path(path) / "clock.sqlite3")
            clock = Clock(db)
            clock.timer.stop()
            instant = datetime(2026, 11, 5, 20, 15, 30, tzinfo=UTC)
            for key, expected_time, expected_date in (
                ("moscow", "23:15:30", "05.11.2026"),
                ("petersburg", "23:15:30", "05.11.2026"),
                ("irkutsk", "04:15:30", "06.11.2026"),
                ("ulan_ude", "04:15:30", "06.11.2026"),
            ):
                clock.city.setCurrentIndex(clock.city.findData(key))
                clock.update_clock(instant)
                self.assertEqual((clock.time.text(), clock.date.text()), (expected_time, expected_date))
            reopened = Clock(db)
            reopened.timer.stop()
            self.assertEqual(reopened.city.currentData(), "ulan_ude")

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
