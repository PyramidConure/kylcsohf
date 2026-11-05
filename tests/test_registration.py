import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from creator_control.db import Database
from creator_control.registration import Registration, SIGNUP_URLS
from creator_control.ui.main import Window
from tests.test_core import MemoryVault, account_data


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = Database(self.root / "test.sqlite3")
        self.vault = MemoryVault()
        self.manager = Registration(self.db, self.vault)

    def test_credentials_remain_separate_from_database_and_are_not_replaced(self):
        account = self.db.save_account(account_data("YouTube"))
        record = self.manager.prepare_credentials(account)
        self.assertTrue(record["login"].endswith("@gmail.com"))
        self.assertFalse(record["registered"])
        self.assertEqual(len(record["password"]), 24)
        self.assertEqual(self.manager.prepare_credentials(account), record)
        with self.db.connect() as cx:
            dump = "\n".join(cx.iterdump())
        self.assertNotIn(record["password"], dump)
        self.assertEqual(self.db.account(account)["profile"], "")
        self.assertIsNone(self.db.account(account)["remote_id"])
        existing = account_data()
        existing["profile"] = "https://www.instagram.com/example/"
        identifier = self.db.save_account(existing)
        with self.assertRaisesRegex(ValueError, "существующего"):
            self.manager.prepare_credentials(identifier)

    def test_batch_recovers_after_vault_failure_without_duplicate_cards(self):
        with patch.object(self.vault, "put", side_effect=OSError("temporary vault failure")):
            with self.assertRaises(OSError):
                self.manager.batch("Instagram", "NOVA", "AI model", 2, "same-batch")
        before = self.db.accounts()
        self.assertEqual(len(before), 2)
        identifiers = self.manager.batch("Instagram", "NOVA", "AI model", 2, "same-batch")
        self.assertEqual(self.db.accounts(), before)
        self.assertEqual(self.manager.batch("Instagram", "NOVA", "AI model", 2, "same-batch"), identifiers)
        self.assertNotEqual(self.manager.credentials(identifiers[0])["password"],
                            self.manager.credentials(identifiers[1])["password"])
        for identifier in identifiers:
            self.assertEqual(len(self.db.tasks(identifier)), 22)
            self.assertTrue(all(not task["done"] for task in self.db.tasks(identifier)))
        with self.assertRaises(ValueError):
            self.manager.batch("Instagram", "NOVA", "AI model", 51)

    def test_material_paths_stay_in_workspace_data(self):
        account = self.db.save_account(account_data())
        file = self.root / "avatar.png"
        file.write_bytes(b"fixture")
        self.db.settings({f"account_materials/{account}": json.dumps({"avatar": "avatar.png"})})
        self.assertEqual(self.manager.material_path(account, "avatar"), file)
        self.db.settings({f"account_materials/{account}": json.dumps({"avatar": "../outside.png"})})
        with self.assertRaises(ValueError):
            self.manager.material_path(account, "avatar")

    def test_chat_agent_command_works_without_api_key_and_restores_package(self):
        app = QApplication.instance() or QApplication([])
        account = self.db.save_account(account_data("YouTube"))
        record = self.manager.prepare_credentials(account)
        window = Window(self.db, self.vault)
        self.addCleanup(window.close)
        errors = []
        window.error = errors.append
        window.show()
        window.navigate("chat")
        chat = window.page("chat")
        chat.editor.setPlainText("/аккаунты")
        self.assertTrue(chat.send_button.isEnabled())
        chat.send_button.click()
        self.assertTrue(chat.preparation_open)
        panel = chat.registration
        self.assertEqual(panel.login.text(), record["login"])
        self.assertEqual(panel.secret.echoMode(), QLineEdit.EchoMode.Password)
        self.assertIn("не подтверждены", panel.state.text())
        with patch("creator_control.ui.registration.QDesktopServices.openUrl") as opening:
            panel.signup.click()
            self.assertEqual(opening.call_args.args[0].toString(), SIGNUP_URLS["YouTube"])
        self.assertFalse(panel.profile.isEnabled())
        panel.form.set({"platform": "Instagram", "count": 2, "title": "Оригинальный проект", "topic": "AI model"})
        panel.create_button.click()
        for _ in range(150):
            if window.worker is None:
                break
            QTest.qWait(20)
        self.assertIsNone(window.worker)
        self.assertEqual(len(self.db.accounts()), 3)
        self.assertEqual(self.db.query("SELECT * FROM chat_messages"), [])
        window.close()
        reopened = Window(self.db, self.vault)
        self.addCleanup(reopened.close)
        reopened.show()
        reopened.navigate("chat")
        restored = reopened.page("chat")
        self.assertTrue(restored.preparation_open)
        self.assertEqual(restored.registration.form.values()["count"], 2)
        self.assertEqual(restored.registration.batch_id, panel.batch_id)
        self.assertEqual(errors, [])
        app.processEvents()
        reopened.close()
