import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from creator_control.chat import ChatService, ChatStore, DEFAULT_MODEL, KEY_NAME, saved_models
from creator_control.db import Database, restore_database
from creator_control.platforms import APIError
from creator_control.ui.main import Window
from tests.test_core import MemoryVault, account_data


def answer(text):
    return {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    ]}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = Database(self.root / "chat.sqlite3")
        self.store = ChatStore(self.db)
        self.vault = MemoryVault()
        self.vault.put(KEY_NAME, "dummy-api-key-for-local-tests")
        self.http = Mock()
        self.service = ChatService(self.db, self.vault, self.http)

    def test_migration_preserves_accounts_and_tasks(self):
        account = self.db.save_account(account_data())
        before = self.db.tasks(account)
        with self.db.connect() as cx:
            cx.executescript("DROP TABLE chat_messages; DROP TABLE chats; PRAGMA user_version=1;")
        migrated = Database(self.db.path)
        self.assertEqual(migrated.tasks(account), before)
        self.assertEqual(migrated.account(account)["name"], "Тест")
        chat = ChatStore(migrated).create(account)
        self.assertEqual(ChatStore(migrated).messages(chat), [])

    def test_context_history_and_key_are_kept_separate(self):
        account = self.db.save_account(account_data())
        chat = self.store.create(account)
        self.http.json.side_effect = [answer("Идея первого ролика"), answer("Продолжение плана")]
        self.service.send(chat, "Предложи идею")
        self.service.send(chat, "Продолжи")
        request = self.http.json.call_args.kwargs
        self.assertEqual(request["json"]["model"], DEFAULT_MODEL)
        self.assertFalse(request["json"]["store"])
        history = request["json"]["input"]
        self.assertIn("Справочный контекст", history[0]["content"])
        self.assertEqual([row["role"] for row in history], ["user", "user", "assistant", "user"])
        self.assertEqual(history[-2]["content"], "Идея первого ролика")
        with self.db.connect() as cx:
            dump = "\n".join(cx.iterdump())
        self.assertNotIn(self.vault[KEY_NAME], dump)
        self.assertEqual(len(self.store.messages(chat)), 4)

    def test_existing_chats_survive_model_schema_upgrade(self):
        chat = self.store.create()
        message = self.store.begin(chat, "Старый вопрос")
        self.store.finish(message, "Старый ответ")
        with self.db.connect() as cx:
            cx.executescript("ALTER TABLE chats DROP COLUMN model; "
                             "ALTER TABLE chat_messages DROP COLUMN model; PRAGMA user_version=2;")
        reopened = ChatStore(Database(self.db.path))
        self.assertEqual([row["body"] for row in reopened.messages(chat)], ["Старый вопрос", "Старый ответ"])
        self.assertEqual([row["model"] for row in reopened.messages(chat)], ["", ""])

    def test_failed_requests_are_preserved_but_not_replayed(self):
        chat = self.store.create()
        self.http.json.side_effect = [APIError("OpenAI", 429), answer("Готовый план")]
        with self.assertRaisesRegex(ValueError, "лимиты"):
            self.service.send(chat, "Сделай план")
        self.assertEqual(self.store.messages(chat)[0]["state"], "failed")
        self.service.send(chat, "Сделай план")
        history = self.http.json.call_args.kwargs["json"]["input"]
        self.assertEqual(history, [{"role": "user", "content": "Сделай план"}])
        self.assertEqual(len(self.store.messages(chat)), 3)

    def test_changing_models_keeps_old_answers_and_model_choices(self):
        chat = self.store.create(model="gpt-4o")
        self.http.json.side_effect = [answer("Первый ответ"), answer("Ответ другой модели"),
                                     {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-image-1"}]}]
        self.service.send(chat, "Первый вопрос")
        self.assertEqual(self.http.json.call_args.kwargs["json"]["model"], "gpt-4o")
        self.store.set_model(chat, DEFAULT_MODEL)
        self.service.send(chat, "Второй вопрос")
        messages = self.store.messages(chat)
        self.assertEqual([(row["body"], row["model"]) for row in messages if row["role"] == "assistant"],
                         [("Первый ответ", "gpt-4o"), ("Ответ другой модели", DEFAULT_MODEL)])
        models = self.service.catalog()
        self.assertIn("gpt-4o", models)
        self.assertIn(DEFAULT_MODEL, models)
        self.assertNotIn("gpt-image-1", models)
        reopened = Database(self.db.path)
        self.assertIn("gpt-4o", saved_models(reopened))
        self.assertEqual(ChatStore(reopened).messages(chat), messages)

    def test_missing_key_and_recovery_do_not_send_requests(self):
        chat = self.store.create()
        self.vault.remove(KEY_NAME)
        with self.assertRaisesRegex(ValueError, "API-ключ"):
            self.service.send(chat, "Привет")
        self.http.json.assert_not_called()
        self.assertEqual(self.store.messages(chat), [])
        message = self.store.begin(chat, "Сохранённый вопрос")
        with self.assertRaises(ValueError):
            self.store.begin(chat, "Второй одновременный запрос")
        self.store.recover()
        self.assertEqual(self.store.messages(chat)[0]["state"], "failed")
        with self.assertRaises(ValueError):
            self.store.finish(message, "Запоздалый ответ")
        backup = self.db.backup(self.root / "backup.sqlite3")
        restore_database(backup, self.root / "restored.sqlite3")
        restored = Database(self.root / "restored.sqlite3")
        self.assertEqual(ChatStore(restored).messages(chat)[0]["body"], "Сохранённый вопрос")

    def test_chat_stays_responsive_and_saves_reply(self):
        app = QApplication.instance() or QApplication([])
        window = Window(self.db, self.vault)
        self.addCleanup(window.close)
        errors = []
        window.error = errors.append
        window.show()
        window.navigate("chat")
        page = window.stack.currentWidget()
        release = Event()
        self.addCleanup(release.set)

        def respond(*_args, **_kwargs):
            if not release.wait(5):
                raise ValueError("Timed out")
            return answer("Проверочный ответ")

        page.service.http = Mock()
        page.service.http.json.side_effect = respond
        welcome = page.canvas.findChild(QWidget, "chatWelcome")
        self.assertIsNotNone(welcome)
        page.editor.setPlainText("Составь короткий план")
        page.send_button.click()
        self.assertTrue(welcome.isHidden())
        self.assertTrue(page.busy)
        self.assertTrue(window.navigation.isEnabled())
        window.navigate("overview")
        window.navigate("chat")
        release.set()
        for _ in range(150):
            if not page.busy:
                break
            QTest.qWait(20)
        self.assertFalse(page.busy)
        self.assertEqual(page.editor.toPlainText(), "")
        self.assertEqual(page.store.messages(page.chat_id)[-1]["body"], "Проверочный ответ")
        self.assertEqual(page.sessions.count(), 1)
        self.assertEqual(errors, [])
        window.close()
