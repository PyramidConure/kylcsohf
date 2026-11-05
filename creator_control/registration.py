"""Reusable local registration preparation. No external account creation."""

from datetime import datetime
import json
from pathlib import Path
import re
import secrets
import string
from uuid import uuid4
from zoneinfo import ZoneInfo

from .db import now

SIGNUP_URLS = {"YouTube": "https://accounts.google.com/signup",
               "Instagram": "https://www.instagram.com/accounts/emailsignup/"}


class Registration:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault

    def credentials(self, account_id):
        if self.db.account(account_id) is None:
            raise ValueError("Выберите сохранённую карточку аккаунта.")
        return self.vault.get(f"registration/{account_id}")

    def prepare_credentials(self, account_id, prefix="nova.virtual.studio", login=None):
        account = self.db.account(account_id)
        if account is None:
            raise ValueError("Карточка не найдена.")
        current = self.credentials(account_id)
        if current:
            return current
        if account["remote_id"] or account["profile"]:
            raise ValueError("Для существующего профиля не создаётся новый пароль. Используйте его обычный вход.")
        prefix = re.sub(r"[^a-z0-9.]", "", prefix.lower()).strip(".")[:20].rstrip(".") or "creator.studio"
        prefix = re.sub(r"\.+", ".", prefix)
        handle = f"{prefix}.{secrets.randbelow(900000) + 100000}"
        generated_login = handle + "@gmail.com" if account["platform"] == "YouTube" else handle
        if login is not None:
            pattern = r"[a-zA-Z0-9.]{6,30}@gmail\.com" if account["platform"] == "YouTube" else r"[a-zA-Z0-9._]{1,30}"
            if not re.fullmatch(pattern, login):
                raise ValueError("Проверьте предложенный логин.")
            generated_login = login
        alphabet = string.ascii_letters + string.digits + "!@_-"
        letters = [secrets.choice(group) for group in (string.ascii_lowercase, string.ascii_uppercase,
                                                      string.digits, "!@_-")]
        letters += [secrets.choice(alphabet) for _ in range(20)]
        secrets.SystemRandom().shuffle(letters)
        record = {"login": generated_login, "password": "".join(letters), "created": now(), "registered": False}
        self.vault.put(f"registration/{account_id}", record)
        return record

    def batch(self, platform, title, topic, count, batch_id=None):
        if platform not in SIGNUP_URLS or not title.strip() or not topic.strip() or not 1 <= count <= 50:
            raise ValueError("Укажите платформу, название, тематику и количество от 1 до 50.")
        batch_id = batch_id or uuid4().hex
        key = f"registration_batch/{batch_id}"
        config = {"platform": platform, "title": title.strip(), "topic": topic.strip(), "count": count}
        stored = json.loads(self.db.setting(key, "null"))
        if stored is None:
            stored = {"config": config, "accounts": [], "created": now()}
            self.db.settings({key: json.dumps(stored, ensure_ascii=False)})
        elif stored["config"] != config:
            raise ValueError("Этот пакет уже создан с другими параметрами. Начните новый пакет.")
        today = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
        for index in range(len(stored["accounts"]), count):
            account_id = self.db.save_account(dict(
                platform=platform, name=f"{title.strip()} · {platform} · {index + 1:02}", profile="", created=today,
                start=today, kind="Новый", topic=topic.strip(), country="US", language="English",
                timezone="America/New_York", duration=14, owner="Владелец", emulator=None,
                subtopics="", keywords="", ideas="Локальная подготовка. Внешняя регистрация ещё не подтверждена.",
                prep_override="",
            ))
            stored["accounts"].append(account_id)
            self.db.settings({key: json.dumps(stored, ensure_ascii=False)})
            if platform == "YouTube":
                for task in ("Создать собственный Google-аккаунт и YouTube-канал",
                             "Подтвердить контакты и настроить двухфакторную аутентификацию",
                             "Подготовить и установить оформление канала",
                             "Создать оригинальные видео и контент-план",
                             "Подключить собственный канал через OAuth"):
                    self.db.add_task(account_id, task, today)
        for account_id in stored["accounts"]:
            self.prepare_credentials(account_id, title)
        return stored["accounts"]

    def materials(self, account_id):
        try:
            value = json.loads(self.db.setting(f"account_materials/{account_id}", "{}"))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def material_path(self, account_id, field):
        value = self.materials(account_id).get(field)
        if not isinstance(value, str) or not value:
            return None
        root = self.db.path.parent.resolve()
        path = (root / value).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise ValueError("Файл подготовки отсутствует в рабочем каталоге данных.")
        return path
