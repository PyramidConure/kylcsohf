"""Local chat history and the official OpenAI Responses API."""

import json
import re
import sqlite3

from .db import now
from .platforms import APIError, Transport

DEFAULT_MODEL = "gpt-6.1-sol"
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

    def create(self, account_id=None, model=None):
        stamp = now()
        model = validate_model(model or self.db.setting("chat_model", DEFAULT_MODEL))
        with self.db.connect() as cx:
            return cx.execute("INSERT INTO chats(title,account_id,created,updated,model) VALUES (?,?,?,?,?)",
                              ("Новый чат", account_id, stamp, stamp, model)).lastrowid

    def set_model(self, identifier, model):
        model = validate_model(model)
        with self.db.connect() as cx:
            if not cx.execute("""UPDATE chats SET model=? WHERE id=? AND NOT EXISTS (
                                SELECT 1 FROM chat_messages WHERE chat_id=chats.id AND state='pending')""",
                              (model, identifier)).rowcount:
                raise ValueError("Дождитесь текущего ответа перед сменой модели.")
        remember_models(self.db, [model])

    def rename(self, identifier, title):
        title = title.strip()[:120]
        if not title:
            raise ValueError("Введите название беседы.")
        with self.db.connect() as cx:
            cx.execute("UPDATE chats SET title=? WHERE id=?", (title, identifier))

    def messages(self, identifier):
        return self.db.query("SELECT * FROM chat_messages WHERE chat_id=? ORDER BY id", identifier)

    def matching_chats(self, text):
        needle = text.casefold().strip()
        rows = self.db.query("""SELECT c.id,c.title,a.name AS account,m.body FROM chats c
                                LEFT JOIN accounts a ON a.id=c.account_id
                                LEFT JOIN chat_messages m ON m.chat_id=c.id""")
        return {row["id"] for row in rows if needle in "\n".join(
            str(row[key] or "") for key in ("title", "account", "body")).casefold()}

    def draft(self, identifier):
        try:
            data = json.loads(self.db.setting(f"chat_draft/{identifier or 'new'}", "{}"))
        except ValueError:
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("body", ""), str):
            data = {}
        return {"body": data.get("body", ""), "account_id": data.get("account_id")}

    def save_draft(self, identifier, text, account_id=None):
        key = f"chat_draft/{identifier or 'new'}"
        with self.db.connect() as cx:
            if text or account_id is not None:
                cx.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(
                    {"body": text, "account_id": account_id}, ensure_ascii=False)))
            else:
                cx.execute("DELETE FROM settings WHERE key=?", (key,))

    def export_markdown(self, identifier):
        chat = self.db.get("SELECT c.*,a.name AS account FROM chats c LEFT JOIN accounts a "
                           "ON a.id=c.account_id WHERE c.id=?", identifier)
        if chat is None:
            raise ValueError("Выберите беседу для экспорта.")
        parts = ["# " + chat["title"], "Создано: " + chat["created"]]
        if chat["account"]:
            parts.append("Аккаунт: " + chat["account"])
        for message in self.messages(identifier):
            author = "Вы" if message["role"] == "user" else message["model"] or "ИИ-помощник"
            parts.extend(["---", "## " + author + " · " + message["created"], message["body"]])
            if message["state"] != "completed":
                parts.append("Статус: " + (message["error"] or "Ожидается ответ"))
        return "\n\n".join(parts) + "\n"

    def begin(self, identifier, text, model=None):
        text = text.strip()
        if not text or len(text) > 16_000:
            raise ValueError("Введите сообщение длиной до 16 000 символов.")
        with self.db.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            chat = cx.execute("SELECT title,model FROM chats WHERE id=?", (identifier,)).fetchone()
            if not chat:
                raise ValueError("Чат не найден.")
            size = cx.execute("""SELECT COALESCE(SUM(length(body)),0) FROM chat_messages
                                 WHERE chat_id=? AND state='completed'""", (identifier,)).fetchone()[0]
            if size + len(text) > 120_000:
                raise ValueError("Переписка стала большой. Начните новый чат, сохранив нужные выводы.")
            model = validate_model(model or chat["model"] or self.db.setting("chat_model", DEFAULT_MODEL))
            try:
                message = cx.execute("""INSERT INTO chat_messages(chat_id,role,body,state,created,model)
                                        VALUES (?,'user',?,'pending',?,?)""",
                                     (identifier, text, now(), model)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("В этом чате уже ожидается ответ.") from None
            title = " ".join(text.split())[:55] if chat["title"] == "Новый чат" else chat["title"]
            cx.execute("UPDATE chats SET title=?,updated=?,model=? WHERE id=?", (title, now(), model, identifier))
            return message

    def finish(self, message_id, answer, model=None):
        with self.db.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            row = cx.execute("SELECT chat_id,model FROM chat_messages WHERE id=? AND state='pending' AND role='user'",
                             (message_id,)).fetchone()
            if not row:
                raise ValueError("Состояние запроса изменилось.")
            cx.execute("UPDATE chat_messages SET state='completed' WHERE id=?", (message_id,))
            cx.execute("""INSERT INTO chat_messages(chat_id,role,body,state,reply_to,created,model)
                          VALUES (?,'assistant',?,'completed',?,?,?)""",
                       (row["chat_id"], answer, message_id, now(), model or row["model"]))
            cx.execute("UPDATE chats SET updated=? WHERE id=?", (now(), row["chat_id"]))

    def fail(self, message_id, error):
        with self.db.connect() as cx:
            cx.execute("UPDATE chat_messages SET state='failed',error=? WHERE id=? AND state='pending'",
                       (error, message_id))

    def recover(self):
        with self.db.connect() as cx:
            cx.execute("""UPDATE chat_messages SET state='failed',error='Запрос прерван закрытием программы.'
                          WHERE state='pending'""")


def validate_model(model):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}", model):
        raise ValueError("Укажите корректное имя GPT-модели.")
    return model


def saved_models(db):
    try:
        saved = json.loads(db.setting("chat_saved_models", "[]"))
    except ValueError:
        saved = []
    if not isinstance(saved, list):
        saved = []
    rows = db.query("SELECT model FROM chats UNION SELECT model FROM chat_messages")
    values = [DEFAULT_MODEL, db.setting("chat_model", DEFAULT_MODEL), *saved, *(row["model"] for row in rows)]
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and
                              re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}", value)))


def remember_models(db, models):
    merged = list(dict.fromkeys([*saved_models(db), *(validate_model(model) for model in models)]))
    db.settings({"chat_saved_models": json.dumps(merged)})
    return merged


def chat_config(db, vault):
    key = vault.get(KEY_NAME)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Добавьте OpenAI API-ключ в «Настройки → ИИ-чат».")
    model = validate_model(db.setting("chat_model", DEFAULT_MODEL))
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
        chat = self.db.get("SELECT account_id,model FROM chats WHERE id=?", chat_id)
        if not chat:
            raise ValueError("Чат не найден.")
        model = validate_model(chat["model"] or model)
        message_id = self.store.begin(chat_id, text, model)
        try:
            history = []
            if chat["account_id"] is not None:
                account = self.db.account(chat["account_id"])
                context = {field: account[field] for field in
                           ("platform", "name", "topic", "country", "language", "timezone",
                            "subtopics", "keywords", "ideas")}
                tasks = self.db.tasks(chat["account_id"])
                context["preparation"] = {
                    "completed": sum(bool(task["done"]) for task in tasks), "total": len(tasks),
                    "next_tasks": [{"title": task["title"], "due": task["due"]}
                                   for task in tasks if not task["done"]][:20],
                    "completed_tasks": [task["title"] for task in tasks if task["done"]][:30],
                }
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
            returned_model = response.get("model")
            actual = returned_model if isinstance(returned_model, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}", returned_model) else model
            self.store.finish(message_id, answer, actual)
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

    def catalog(self):
        key, _model, _limit = chat_config(self.db, self.vault)
        response = self.http.json("GET", "https://api.openai.com/v1/models", service="OpenAI",
                                  headers={"Authorization": "Bearer " + key})
        models = sorted({row["id"] for row in response.get("data", []) if isinstance(row, dict) and
                         isinstance(row.get("id"), str) and
                         re.fullmatch(r"(?:gpt-|chatgpt-|o[1-9])[A-Za-z0-9._:-]*", row["id"]) and
                         not any(part in row["id"] for part in ("audio", "realtime", "transcri", "tts", "image"))})
        return remember_models(self.db, models)
