"""Local chat history and the official OpenAI Responses API."""

import json
import re
import sqlite3

from .db import now
from .platforms import APIError, Transport

DEFAULT_MODEL = "gpt-5.4-mini"
KEY_NAME = "chat/openai"
INSTRUCTIONS = (
    "Ты ИИ-помощник Creator Control Center. Помогай автору с идеями, сценариями, "
    "описаниями и планированием собственного контента. Обычно отвечай по-русски, "
    "а тексты для аудитории создавай на запрошенном языке. "
    "Контекст аккаунта является справочными данными. Различай подготовленный план "
    "и фактически выполненные действия. Твои ответы являются предложениями для автора. "
    "Подготовка аккаунта включает оформление, безопасность и оригинальный контент. "
    "Не предлагай искусственные просмотры, массовые реакции, обход проверок или "
    "передачу паролей. Для виртуальных персонажей учитывай прозрачное обозначение AI-контента."
)


class ChatStore:
    def __init__(self, db):
        self.db = db

    def chats(self):
        return self.db.query("""SELECT c.*,a.name AS account FROM chats c
                                LEFT JOIN accounts a ON a.id=c.account_id ORDER BY c.updated DESC,c.id DESC""")

    def create(self, account_id=None):
        stamp = now()
        with self.db.connect() as cx:
            return cx.execute("INSERT INTO chats(title,account_id,created,updated) VALUES (?,?,?,?)",
                              ("Новый чат", account_id, stamp, stamp)).lastrowid

    def messages(self, identifier):
        return self.db.query("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", identifier)

    def begin(self, identifier, text):
        text = text.strip()
        if not text or len(text) > 16_000:
            raise ValueError("Введите сообщение длиной до 16 000 символов.")
        with self.db.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            chat = cx.execute("SELECT title FROM chats WHERE id=?", (identifier,)).fetchone()
            if not chat:
                raise ValueError("Чат не найден.")
            size = cx.execute("""SELECT COALESCE(SUM(length(body)),0) FROM chat_messages
                                 WHERE chat_id=? AND state='completed'""", (identifier,)).fetchone()[0]
            if size + len(text) > 120_000:
                raise ValueError("Переписка стала большой. Начните новый чат, сохранив нужные выводы.")
            try:
                message = cx.execute("""INSERT INTO chat_messages(chat_id,role,body,state,created)
                                        VALUES (?,'user',?,'pending',?)""", (identifier, text, now())).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("В этом чате уже ожидается ответ.") from None
            title = " ".join(text.split())[:55] if chat["title"] == "Новый чат" else chat["title"]
            cx.execute("UPDATE chats SET title=?,updated=? WHERE id=?", (title, now(), identifier))
            return message

    def finish(self, message_id, answer):
        with self.db.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            row = cx.execute("SELECT chat_id FROM chat_messages WHERE id=? AND state='pending' AND role='user'",
                             (message_id,)).fetchone()
            if not row:
                raise ValueError("Состояние запроса изменилось.")
            cx.execute("UPDATE chat_messages SET state='completed' WHERE id=?", (message_id,))
            cx.execute("""INSERT INTO chat_messages(chat_id,role,body,state,reply_to,created)
                          VALUES (?,'assistant',?,'completed',?,?)""", (row["chat_id"], answer, message_id, now()))
            cx.execute("UPDATE chats SET updated=? WHERE id=?", (now(), row["chat_id"]))

    def fail(self, message_id, error):
        with self.db.connect() as cx:
            cx.execute("UPDATE chat_messages SET state='failed',error=? WHERE id=? AND state='pending'",
                       (error, message_id))

    def recover(self):
        with self.db.connect() as cx:
            cx.execute("""UPDATE chat_messages SET state='failed',error='Запрос прерван закрытием программы.'
                          WHERE state='pending'""")


def chat_config(db, vault):
    key = vault.get(KEY_NAME)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Добавьте OpenAI API-ключ в «Настройки → ИИ-чат».")
    model = db.setting("chat_model", DEFAULT_MODEL)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}", model):
        raise ValueError("Проверьте имя модели в настройках ИИ-чата.")
    limit = int(db.setting("chat_output_limit", "4096"))
    if not 256 <= limit <= 16_000:
        raise ValueError("Проверьте лимит ответа в настройках ИИ-чата.")
    return key, model, limit


class ChatService:
    def __init__(self, db, vault, http=None):
        self.db, self.vault, self.http = db, vault, http or Transport()
        self.store = ChatStore(db)

    def send(self, chat_id, text):
        key, model, limit = chat_config(self.db, self.vault)
        message_id = self.store.begin(chat_id, text)
        try:
            history = []
            chat = self.db.get("SELECT account_id FROM chats WHERE id=?", chat_id)
            if chat["account_id"] is not None:
                account = self.db.account(chat["account_id"])
                context = {field: account[field] for field in
                           ("platform", "name", "topic", "country", "language", "timezone",
                            "subtopics", "keywords", "ideas")}
                history.append({"role": "user", "content": "Справочный контекст выбранного аккаунта\n" +
                                json.dumps(context, ensure_ascii=False)})
            history.extend({"role": row["role"], "content": row["body"]}
                           for row in self.store.messages(chat_id) if row["state"] != "failed")
            response = self.http.json("POST", "https://api.openai.com/v1/responses", service="OpenAI",
                                      headers={"Authorization": "Bearer " + key}, json={
                                          "model": model, "instructions": INSTRUCTIONS,
                                          "input": history, "store": False, "max_output_tokens": limit,
                                      })
            parts = [part.get("text") or part.get("refusal") for item in response.get("output", [])
                     if item.get("type") == "message" for part in item.get("content", [])
                     if part.get("type") in {"output_text", "refusal"}]
            answer = "\n\n".join(text for text in parts if isinstance(text, str) and text.strip())
            if not answer or response.get("status") not in {"completed", "incomplete"}:
                raise ValueError("OpenAI не вернул текст ответа. Сообщение сохранено, можно отправить его повторно.")
            if response["status"] == "incomplete":
                answer += "\n\n*Ответ остановлен до завершения. Можно попросить продолжение.*"
            self.store.finish(message_id, answer)
        except Exception as error:
            if isinstance(error, APIError):
                details = {401: "Проверьте OpenAI API-ключ.", 403: "Проверьте доступ проекта к API.",
                           404: "Проверьте доступность выбранной модели.",
                           429: "Проверьте лимиты и баланс API-проекта."}
                message = f"OpenAI: HTTP {error.status}. " + details.get(
                    error.status, "Запрос прерван. Проверьте подключение и попробуйте снова.")
            else:
                message = str(error) if isinstance(error, ValueError) else "Запрос прерван. История сохранена."
            self.store.fail(message_id, message)
            raise ValueError(message) from None
        return answer
