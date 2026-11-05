import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from .planning import STATUSES, calendar

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL CHECK(platform IN ('Instagram','YouTube')),
    name TEXT NOT NULL, profile TEXT NOT NULL, created TEXT NOT NULL, start TEXT NOT NULL,
    kind TEXT NOT NULL, topic TEXT NOT NULL, country TEXT NOT NULL, language TEXT NOT NULL,
    timezone TEXT NOT NULL, duration INTEGER NOT NULL, owner TEXT NOT NULL,
    emulator INTEGER, subtopics TEXT NOT NULL, keywords TEXT NOT NULL, ideas TEXT NOT NULL,
    prep_override TEXT NOT NULL, remote_id TEXT, remote_name TEXT, added TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS linked_profile ON accounts(platform, remote_id) WHERE remote_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
    template TEXT, phase TEXT NOT NULL, stage TEXT NOT NULL, title TEXT NOT NULL,
    due TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(account_id,template)
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, description TEXT NOT NULL, metadata TEXT NOT NULL, added TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
    video_id INTEGER NOT NULL REFERENCES videos(id), title TEXT NOT NULL, caption TEXT NOT NULL,
    topic TEXT NOT NULL, due TEXT NOT NULL, privacy TEXT NOT NULL,
    for_kids INTEGER NOT NULL, synthetic INTEGER NOT NULL, video_url TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned', remote_id TEXT, container TEXT, result TEXT NOT NULL DEFAULT '',
    started TEXT, remote_account TEXT, added TEXT NOT NULL,
    UNIQUE(account_id,video_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_published_media
    ON publications(account_id,remote_id) WHERE remote_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY, publication_id INTEGER NOT NULL REFERENCES publications(id),
    at TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    account_id INTEGER NOT NULL REFERENCES accounts(id), subject TEXT NOT NULL,
    day TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL,
    PRIMARY KEY(account_id,subject,day,name)
);
PRAGMA user_version = 1;
"""

ACCOUNT_FIELDS = ("platform", "name", "profile", "created", "start", "kind", "topic", "country",
                  "language", "timezone", "duration", "owner", "emulator", "subtopics", "keywords",
                  "ideas", "prep_override")
POST_FIELDS = ("account_id", "video_id", "title", "caption", "topic", "due", "privacy", "for_kids",
               "synthetic", "video_url")


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def https_url(value, hosts=None):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or
            parsed.password is not None or (hosts and parsed.hostname not in hosts) or
            any(c.isspace() or c == chr(92) for c in value)):
        raise ValueError("Укажите корректную ссылку HTTPS.")
    return parsed


class Database:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as cx:
            version = cx.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                cx.execute("PRAGMA journal_mode=WAL")
                cx.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            elif version != 1:
                raise ValueError(f"Версия базы {version} не поддерживается.")

    @contextmanager
    def connect(self):
        cx = sqlite3.connect(self.path, timeout=15)
        cx.row_factory = sqlite3.Row
        try:
            with cx:
                cx.execute("PRAGMA foreign_keys=ON")
                yield cx
        finally:
            cx.close()

    def query(self, sql, *args):
        with self.connect() as cx:
            return [dict(row) for row in cx.execute(sql, args)]

    def get(self, sql, *args):
        return next(iter(self.query(sql, *args)), None)

    def setting(self, key, default=""):
        row = self.get("SELECT value FROM settings WHERE key=?", key)
        return row["value"] if row else default

    def settings(self, values):
        with self.connect() as cx:
            cx.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", values.items())

    def account(self, identifier):
        return self.get("SELECT * FROM accounts WHERE id=?", identifier)

    def accounts(self):
        return self.query("SELECT * FROM accounts ORDER BY platform,name,id")

    def save_account(self, values, identifier=None):
        data = {k: values[k] for k in ACCOUNT_FIELDS}
        data["name"] = data["name"].strip()
        data["country"] = data["country"].strip().upper()
        if data["platform"] not in {"Instagram", "YouTube"} or not data["name"]:
            raise ValueError("Укажите платформу и название аккаунта.")
        if len(data["country"]) != 2 or not data["country"].isascii() or not data["country"].isalpha():
            raise ValueError("Введите двухбуквенный код страны, например US.")
        if data["kind"] not in {"Новый", "Действующий"} or data["prep_override"] not in ("", *STATUSES):
            raise ValueError("Проверьте тип и статус аккаунта.")
        if data["emulator"] is not None and data["emulator"] < 0:
            raise ValueError("Индекс LDPlayer должен быть неотрицательным.")
        if not data["language"].strip():
            raise ValueError("Укажите язык контента.")
        ZoneInfo(data["timezone"])
        date.fromisoformat(data["created"])
        start = date.fromisoformat(data["start"])
        hosts = ({"instagram.com", "www.instagram.com"} if data["platform"] == "Instagram"
                 else {"youtube.com", "www.youtube.com", "m.youtube.com"})
        if data["profile"]:
            https_url(data["profile"], hosts)
        tasks = (list(calendar(start, data["duration"], data["country"]))
                 if data["platform"] == "Instagram" else [])
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            if identifier is None:
                marks = ",".join("?" for _ in data)
                identifier = cx.execute(
                    f"INSERT INTO accounts ({','.join(data)},added) VALUES ({marks},?)",
                    (*data.values(), now()),
                ).lastrowid
            else:
                current = cx.execute("SELECT platform FROM accounts WHERE id=?", (identifier,)).fetchone()
                if not current or current["platform"] != data["platform"]:
                    raise ValueError("Платформа сохранённого аккаунта фиксирована.")
                fields = ",".join(f"{k}=?" for k in data)
                cx.execute(f"UPDATE accounts SET {fields} WHERE id=?", (*data.values(), identifier))
            cx.execute("UPDATE tasks SET active=0 WHERE account_id=? AND template IS NOT NULL", (identifier,))
            cx.executemany(
                """INSERT INTO tasks(account_id,template,phase,stage,title,due) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(account_id,template) DO UPDATE SET
                   phase=excluded.phase, stage=excluded.stage, title=excluded.title, due=excluded.due, active=1""",
                [(identifier, *task) for task in tasks],
            )
        return identifier

    def bind(self, account_id, remote_id, name):
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            current = cx.execute("SELECT remote_id FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not current or current["remote_id"] not in (None, remote_id):
                raise ValueError("Выбран другой профиль. Создайте для него отдельную карточку.")
            try:
                cx.execute("UPDATE accounts SET remote_id=?,remote_name=? WHERE id=?", (remote_id, name, account_id))
            except sqlite3.IntegrityError:
                raise ValueError("Этот профиль уже связан с другой карточкой.") from None

    def tasks(self, account_id=None):
        if account_id is None:
            return self.query("""SELECT t.*, a.name AS account FROM tasks t LEFT JOIN accounts a
                                 ON a.id=t.account_id WHERE active=1 ORDER BY t.due,t.id""")
        return self.query("SELECT * FROM tasks WHERE active=1 AND account_id=? ORDER BY due,id", account_id)

    def add_task(self, account_id, title, due):
        date.fromisoformat(due)
        if not title.strip():
            raise ValueError("Введите название задачи.")
        with self.connect() as cx:
            cx.execute("INSERT INTO tasks(account_id,phase,stage,title,due) VALUES (?,?,?,?,?)",
                       (account_id, "manual", "Вручную", title.strip(), due))

    def complete(self, task_id, done):
        with self.connect() as cx:
            cx.execute("UPDATE tasks SET done=? WHERE id=? AND active=1", (bool(done), task_id))

    def add_video(self, path, digest, metadata):
        path = str(Path(path).resolve(strict=True))
        with self.connect() as cx:
            try:
                return cx.execute(
                    "INSERT INTO videos(path,sha256,title,description,metadata,added) VALUES (?,?,?,?,?,?)",
                    (path, digest, Path(path).stem, "", json.dumps(metadata), now()),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError(f"Путь или содержимое уже есть в каталоге: {Path(path).name}") from None

    def save_video(self, identifier, title, description):
        if not title.strip():
            raise ValueError("Укажите название видео.")
        with self.connect() as cx:
            cx.execute("UPDATE videos SET title=?,description=? WHERE id=?", (title.strip(), description, identifier))

    def relocate_video(self, identifier, path, digest):
        path = str(Path(path).resolve(strict=True))
        with self.connect() as cx:
            try:
                updated = cx.execute("UPDATE videos SET path=? WHERE id=? AND sha256=?",
                                     (path, identifier, digest)).rowcount
            except sqlite3.IntegrityError:
                raise ValueError("Этот путь уже используется другим видео.") from None
            if not updated:
                raise ValueError("Содержимое файла отличается от видео в каталоге.")

    def videos(self):
        return self.query("SELECT * FROM videos ORDER BY id DESC")

    def save_post(self, values, identifier=None):
        data = {key: values[key] for key in POST_FIELDS}
        moment = datetime.fromisoformat(data["due"])
        if moment.utcoffset() is None:
            raise ValueError("Укажите время с часовым поясом.")
        data["due"] = moment.astimezone(UTC).isoformat(timespec="seconds")
        data["title"] = data["title"].strip()
        if not data["title"] or data["privacy"] not in {"private", "public", "unlisted"}:
            raise ValueError("Укажите заголовок и видимость.")
        if data["video_url"]:
            parsed = https_url(data["video_url"])
            if parsed.query or parsed.fragment:
                raise ValueError("Укажите публичный HTTPS-адрес без параметров и фрагмента.")
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            try:
                if identifier is None:
                    marks = ",".join("?" for _ in data)
                    return cx.execute(f"INSERT INTO publications({','.join(data)},added) VALUES ({marks},?)",
                                      (*data.values(), now())).lastrowid
                fields = ",".join(f"{k}=?" for k in data)
                if not cx.execute(f"UPDATE publications SET {fields} WHERE id=? AND state='planned'",
                                  (*data.values(), identifier)).rowcount:
                    raise ValueError("Публикация уже отправляется или отправлена.")
                return identifier
            except sqlite3.IntegrityError:
                raise ValueError("Это видео уже добавлено в очередь выбранного аккаунта.") from None

    def posts(self):
        return self.query("""SELECT p.*, a.name AS account,a.platform,v.path FROM publications p
                             JOIN accounts a ON a.id=p.account_id JOIN videos v ON v.id=p.video_id
                             ORDER BY p.due,p.id""")

    def post(self, identifier):
        return self.get("""SELECT p.*, a.platform,a.remote_id AS bound_id,v.path,v.sha256,v.metadata
                           FROM publications p JOIN accounts a ON a.id=p.account_id
                           JOIN videos v ON v.id=p.video_id WHERE p.id=?""", identifier)

    def claim_post(self, post, expected_remote):
        identifier, account_id = post["id"], post["account_id"]
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            mode = cx.execute("SELECT value FROM settings WHERE key='test_mode'").fetchone()
            if not mode or mode[0] != "0":
                raise ValueError("Включён тестовый режим.")
            current = cx.execute("SELECT * FROM publications WHERE id=?", (identifier,)).fetchone()
            if not current or any(current[field] != post[field] for field in POST_FIELDS):
                raise ValueError("Запись изменилась. Подтвердите отправку заново.")
            if not cx.execute("""UPDATE publications SET state='sending',started=?,remote_account=?
                                 WHERE id=? AND state='planned' AND
                                 EXISTS (SELECT 1 FROM accounts WHERE id=? AND remote_id=?)""",
                              (now(), expected_remote, identifier, account_id, expected_remote)).rowcount:
                raise ValueError("Запись изменена или уже отправляется. Обновите очередь.")
            self._journal(cx, identifier, "Отправка подтверждена пользователем")

    @staticmethod
    def _journal(cx, identifier, message):
        cx.execute("INSERT INTO journal(publication_id,at,message) VALUES (?,?,?)",
                   (identifier, now(), message))

    def post_state(self, identifier, state, message, *, remote_id=None, container=None, expected=None):
        with self.connect() as cx:
            if not cx.execute("""UPDATE publications SET state=?,result=?,remote_id=COALESCE(?,remote_id),
                                 container=COALESCE(?,container) WHERE id=? AND state!='planned'
                                 AND (? IS NULL OR state=?)""",
                              (state, message, remote_id, container, identifier, expected, expected)).rowcount:
                raise ValueError("Состояние записи изменилось. Проверьте результат операции.")
            self._journal(cx, identifier, message)

    def save_reel_container(self, identifier, container):
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            current = cx.execute("SELECT state,container FROM publications WHERE id=?", (identifier,)).fetchone()
            if not current or current["state"] != "sending":
                raise ValueError("Состояние записи изменилось. Проверьте результат операции.")
            if current["container"]:
                return current["container"]
            message = "Контейнер Meta создан"
            cx.execute("UPDATE publications SET container=?,result=? WHERE id=?", (container, message, identifier))
            self._journal(cx, identifier, message)
            return container

    def post_error(self, identifier, message):
        with self.connect() as cx:
            cx.execute("""UPDATE publications SET result=? WHERE id=? AND state IN ('sending','committing')""",
                       (message, identifier))
            self._journal(cx, identifier, message)

    def post_log(self, identifier):
        return self.query("SELECT at,message FROM journal WHERE publication_id=? ORDER BY id DESC", identifier)

    def reset_unpublished_reel(self, identifier):
        with self.connect() as cx:
            if not cx.execute("""UPDATE publications SET state='planned',container=NULL,started=NULL,
                                 remote_account=NULL,result='' WHERE id=? AND state='failed'
                                 AND remote_id IS NULL AND container IS NOT NULL AND EXISTS (
                                     SELECT 1 FROM accounts WHERE id=account_id AND platform='Instagram')
                                 AND NOT EXISTS (SELECT 1 FROM journal WHERE publication_id=publications.id
                                     AND message='Начат единственный запрос media_publish')""", (identifier,)).rowcount:
                raise ValueError("Возврат доступен для отклонённого контейнера без попытки публикации.")
            self._journal(cx, identifier, "Отклонённый контейнер возвращён в очередь")

    def release_restored_draft(self, identifier):
        with self.connect() as cx:
            updated = cx.execute("""UPDATE publications SET state='planned',result=''
                                    WHERE id=? AND state='restored' AND started IS NULL
                                    AND container IS NULL AND remote_id IS NULL""", (identifier,)).rowcount
            if not updated:
                raise ValueError("Вернуть в очередь можно только восстановленный черновик без следов отправки.")
            self._journal(cx, identifier, "Черновик проверен пользователем и возвращён в очередь")

    def log(self, identifier, message):
        with self.connect() as cx:
            self._journal(cx, identifier, message)

    def metrics(self, account_id, subject, day, values):
        date.fromisoformat(day)
        rows = [(account_id, subject, day, key, value) for key, value in values.items()
                if type(value) is int and value >= 0]
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            for key in values:
                cx.execute("DELETE FROM metrics WHERE account_id=? AND subject=? AND day=? AND name=?",
                           (account_id, subject, day, key))
            cx.executemany("INSERT INTO metrics VALUES (?,?,?,?,?)", rows)

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination.exists() or destination == self.path:
            raise ValueError("Выберите новый файл для резервной копии.")
        temporary = destination.with_name(f".{uuid4().hex}.tmp")
        try:
            with self.connect() as source:
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE")
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Резервная копия не прошла проверку целостности.")
                finally:
                    target.close()
            temporary.rename(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination


def restore_database(backup, destination):
    source_path = Path(backup).resolve(strict=True)
    destination = Path(destination).resolve()
    if any(Path(str(destination) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")):
        raise ValueError("Для восстановления укажите новый рабочий каталог.")
    temporary = destination.with_name(f".{uuid4().hex}.tmp")
    try:
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        try:
            if (source.execute("PRAGMA user_version").fetchone()[0] != 1 or
                    source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
                raise ValueError("Выберите целую резервную копию Creator Control Center.")
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        restored = Database(temporary)
        with restored.connect() as cx:
            cx.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                           (("test_mode", "1"), ("workspace_id", uuid4().hex)))
            result = "Восстановлено из копии. Сверьте запись с аккаунтом на платформе."
            affected = cx.execute("""UPDATE publications SET state='restored',result=?
                                     WHERE state IN ('planned','sending','committing')
                                     OR (state='failed' AND remote_id IS NULL) RETURNING id""", (result,))
            for row in affected.fetchall():
                restored._journal(cx, row["id"], result)
        temporary.rename(destination)
    except sqlite3.DatabaseError:
        raise ValueError("Файл не является резервной копией Creator Control Center.") from None
    finally:
        temporary.unlink(missing_ok=True)


def report_period(days, today=None):
    end = today or date.today()
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()
