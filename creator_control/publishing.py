import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import https_url, report_period
from .platforms import api_for, count, valid_id
from .tools import sha256

LABELS = {
    "planned": "В очереди", "sending": "Можно продолжить", "committing": "Проверить результат",
    "submitted": "Принято API", "confirmed": "Обработано платформой", "failed": "Отклонено",
    "restored": "Сверить после восстановления",
}
METRICS = {
    "views": "Просмотры", "reach": "Охват", "total_interactions": "Взаимодействия",
    "likes": "Лайки", "comments": "Комментарии", "shares": "Репосты", "saved": "Сохранения",
    "followers": "Подписчики", "followers_change": "Изменение подписчиков",
    "subscribersGained": "Подписались", "subscribersLost": "Отписались",
}


def check_media(post):
    data = json.loads(post["metadata"])
    width, height, seconds = (data.get(key) for key in ("width", "height", "seconds"))
    if not width or not height or width > height or not seconds:
        raise ValueError("Нужны вертикальное или квадратное видео и известная длительность.")
    if post["platform"] == "Instagram":
        if not 3 <= seconds <= 900 or data["video"] not in {"h264", "hevc"} or data["audio"] not in (None, "aac"):
            raise ValueError("Для Reels проверьте 3–900 секунд, видео H.264/HEVC и звук AAC.")
        if not post["video_url"]:
            raise ValueError("Добавьте публичный HTTPS-адрес файла для загрузки Meta.")
        https_url(post["video_url"])
        if len(post["caption"]) > 2200:
            raise ValueError("Описание Reels превышает 2200 символов.")
    elif seconds > 180 or len(post["title"]) > 100 or len(post["caption"].encode("utf-8")) > 5000:
        raise ValueError("Проверьте длительность Shorts до 180 с, заголовок до 100 символов и описание до 5000 байт.")
    path = Path(post["path"])
    if not path.is_file():
        raise ValueError("Файл отсутствует или изменился после импорта.")
    if post["platform"] == "Instagram":
        if (width > 1920 or path.stat().st_size > 1_000_000_000 or
                not {"mov", "mp4"}.intersection(data.get("container", "").split(",")) or
                not 23 <= (data.get("fps") or 0) <= 60 or
                (data.get("audio") and data.get("audio_rate") != 48000) or
                (data.get("video_bitrate") or 0) > 25_000_000):
            raise ValueError("Проверьте формат MOV/MP4, 23–60 кадр/с, до 1920 px по ширине, "
                             "AAC 48 кГц и размер до 1 ГБ. Можно подготовить MP4 9:16.")
    if sha256(path) != post["sha256"]:
        raise ValueError("Файл отсутствует или изменился после импорта.")
    return path


class Publisher:
    def __init__(self, db, vault, http=None):
        self.db, self.vault, self.http = db, vault, http

    def client(self, account_id, expected=None):
        account = self.db.account(account_id)
        client = api_for(account, self.db, self.vault, self.http)
        identifier, _ = client.identity()
        if identifier != account["remote_id"] or (expected is not None and identifier != expected):
            raise ValueError("Авторизован другой профиль. Проверьте подключение аккаунта.")
        return client, identifier

    def send(self, identifier, resume=False):
        post = self.db.post(identifier)
        if not post or post["state"] != ("sending" if resume else "planned"):
            raise ValueError("Выберите запись в очереди или запись для продолжения.")
        path = check_media(post)
        if self.db.setting("test_mode", "1") != "0":
            if resume:
                raise ValueError("Тестовый режим блокирует продолжение отправки.")
            return "Тест пройден. Файл и запись проверены локально."
        if not resume and datetime.fromisoformat(post["due"]) > datetime.now(UTC):
            raise ValueError("Запланированное время ещё не наступило.")
        client, remote = self.client(post["account_id"], post["remote_account"] if resume else post["bound_id"])
        if not resume:
            self.db.claim_post(post, remote)
        try:
            if post["platform"] == "YouTube":
                self._youtube(post, client, path)
            else:
                self._instagram(post, client, remote)
        except Exception as error:
            # Keep the last committed state, including an ambiguous Meta publish.
            self.db.post_error(identifier, self._safe_error(error))
            raise
        return self.db.post(identifier)["result"]

    @staticmethod
    def _safe_error(error):
        from .platforms import APIError
        return str(error) if isinstance(error, APIError) else f"Операция прервана ({type(error).__name__})."

    def _youtube(self, post, client, path):
        identifier = post["id"]
        key = f"upload/{identifier}"
        session = self.vault.get(key)
        if not session and post["container"]:
            raise ValueError("Сессия загрузки потеряна. Проверьте YouTube Studio перед любыми дальнейшими действиями.")
        with path.open("rb") as source:
            size = source.seek(0, 2)
            if not session:
                session = client.start_upload(post, size)
                self.vault.put(key, session)
            if not post["container"]:
                self.db.post_state(identifier, "sending", "Сессия YouTube сохранена",
                                   container="youtube", expected="sending")
            offset, video = client.upload_state(session, size)
            while not video:
                source.seek(offset)
                chunk = source.read(8 * 1024 * 1024)
                if not chunk:
                    raise ValueError("Не удалось прочитать фрагмент файла.")
                received, video = client.upload_state(session, size, offset, chunk)
                if not video and not offset < received <= offset + len(chunk):
                    raise ValueError("YouTube не подтвердил ожидаемый диапазон. Продолжите после проверки сессии.")
                offset = received
        self._remember_youtube(identifier, video)

    def _remember_youtube(self, identifier, video):
        self.db.post_state(identifier, "submitted", "YouTube принял видео. Проверьте обработку и видимость.",
                           remote_id=video, expected="sending")
        try:
            self.vault.remove(f"upload/{identifier}")
        except OSError:
            pass

    def _instagram(self, post, client, remote):
        identifier = post["id"]
        container = post["container"]
        if not container:
            container = self.db.save_reel_container(
                identifier, client.create_reel(remote, post["video_url"], post["caption"]))
        status = client.container_status(container)
        if status == "PUBLISHED":
            self.db.post_state(identifier, "confirmed", "Meta подтвердила публикацию. ID медиа не получен.",
                               expected="sending")
            return
        if status in ("ERROR", "EXPIRED"):
            self.db.post_state(identifier, "failed", f"Контейнер Meta: {status}.", expected="sending")
            return
        if status != "FINISHED":
            self.db.post_state(identifier, "sending", "Meta обрабатывает файл. Проверьте статус или продолжите позже.",
                               expected="sending")
            return
        self.db.post_state(identifier, "committing", "Начат единственный запрос media_publish", expected="sending")
        video = client.publish_reel(remote, container)
        self.db.post_state(identifier, "confirmed", "Meta опубликовала Reels", remote_id=video, expected="committing")

    def check(self, identifier):
        try:
            return self._check(identifier)
        except Exception as error:
            if self.db.post(identifier):
                self.db.log(identifier, "Проверка статуса: " + self._safe_error(error))
            raise

    def _check(self, identifier):
        post = self.db.post(identifier)
        if not post or post["state"] == "planned":
            raise ValueError("Выберите отправленную запись.")
        if post["state"] == "restored" and not (post["platform"] == "Instagram" and post["container"]):
            return "Сверьте запись на платформе. Найденную публикацию можно связать по ID."
        client, remote = self.client(post["account_id"], post["remote_account"])
        if post["platform"] == "YouTube" and post["remote_id"]:
            data = client.video(post["remote_id"], remote)
            status = data.get("status", {})
            upload = status.get("uploadStatus")
            processing = data.get("processingDetails", {}).get("processingStatus")
            visibility = status.get("privacyStatus")
            allowed_status = {"deleted", "failed", "processed", "rejected", "uploaded"}
            upload_label = upload if upload in allowed_status else "недоступно"
            privacy_label = visibility if visibility in {"private", "public", "unlisted"} else "недоступно"
            if upload in {"deleted", "failed", "rejected"} or processing == "failed":
                state = "failed"
            elif upload == "processed" or processing == "succeeded":
                state = "confirmed"
            else:
                state = post["state"] if upload not in allowed_status and not processing else "submitted"
            processing_label = processing if processing in {"failed", "processing", "succeeded", "terminated"} else "недоступно"
            self.db.post_state(identifier, state,
                               f"YouTube: {upload_label}, обработка {processing_label}, видимость {privacy_label}.",
                               expected=post["state"])
        elif post["platform"] == "YouTube" and post["state"] == "sending":
            session = self.vault.get(f"upload/{identifier}")
            if not session:
                return ("Сессия недоступна. Сверьте результат в YouTube Studio." if post["container"]
                        else "Сессия ещё не создана. Для загрузки используйте «Продолжить».")
            path = check_media(post)
            size = path.stat().st_size
            received, video = client.upload_state(session, size)
            if not video:
                return f"YouTube подтвердил {received} из {size} байт. Можно продолжить загрузку."
            self._remember_youtube(identifier, video)
        elif post["platform"] == "Instagram" and post["remote_id"]:
            client.reel(post["remote_id"])
            self.db.post_state(identifier, "confirmed", "Reels доступен по ID публикации.",
                               expected=post["state"])
        elif post["platform"] == "Instagram" and post["container"]:
            status = client.container_status(post["container"])
            if status == "PUBLISHED":
                self.db.post_state(identifier, "confirmed", "Meta подтвердила публикацию.", expected=post["state"])
            elif status in ("ERROR", "EXPIRED") and post["state"] == "sending":
                self.db.post_state(identifier, "failed", f"Контейнер Meta: {status}.", expected="sending")
            else:
                label = status if status in {"IN_PROGRESS", "FINISHED", "ERROR", "EXPIRED"} else "недоступно"
                return f"Контейнер Meta: {label}. " + (
                    "Сверьте публикацию в Instagram. Повторная отправка заблокирована."
                    if post["state"] in {"committing", "restored"} else "Продолжите после обработки файла.")
        else:
            return "Для проверки сессии загрузки используйте «Продолжить»."
        return self.db.post(identifier)["result"]

    def associate(self, identifier, remote_id):
        post = self.db.post(identifier)
        if not post or post["remote_id"] or not (post["state"] == "restored" or
                (post["platform"] == "Instagram" and post["state"] == "confirmed")):
            raise ValueError("Выберите запись для сверки, у которой ещё нет ID публикации.")
        remote_id = valid_id(remote_id, post["platform"])
        if self.db.get("""SELECT id FROM publications WHERE account_id=? AND remote_id=? AND id!=?""",
                       post["account_id"], remote_id, identifier):
            raise ValueError("Этот ID уже связан с другой публикацией.")
        client, remote = self.client(post["account_id"], post["remote_account"])
        if post["platform"] == "YouTube":
            client.video(remote_id, remote)
            self.db.post_state(identifier, "submitted", "ID проверен в YouTube. Проверьте обработку и видимость.",
                               remote_id=remote_id, expected="restored")
            return
        after = None
        for _ in range(20):
            params = {"fields": "id,media_product_type", "limit": 50}
            if after:
                params["after"] = after
            data = client.call("GET", f"/{remote}/media", params=params)
            if any(item.get("id") == remote_id and item.get("media_product_type") == "REELS"
                   for item in data.get("data", [])):
                self.db.post_state(identifier, "confirmed", "ID проверен в списке Reels аккаунта.",
                                   remote_id=remote_id, expected=post["state"])
                return
            paging = data.get("paging", {})
            after = paging.get("cursors", {}).get("after")
            if not paging.get("next") or not after:
                break
        raise ValueError("Reels с указанным ID отсутствует в списке медиа аккаунта.")

    def collect(self, account_id, days):
        if days not in (7, 30):
            raise ValueError("Выберите период 7 или 30 дней.")
        client, remote = self.client(account_id)
        account = self.db.account(account_id)
        timezone = reporting_zone(account)
        today = datetime.now(timezone).date()
        start, end = report_period(days, today - timedelta(days=1))
        day = today.isoformat()
        if account["platform"] == "YouTube":
            counts = client.account_metrics(start, end)
            stats = client.channel().get("statistics", {})
            followers = None if stats.get("hiddenSubscriberCount") else count(stats.get("subscriberCount"))
        else:
            since = datetime.combine(date.fromisoformat(start), time.min, timezone)
            until = datetime.combine(today, time.min, timezone)
            counts = client.account_metrics(int(since.timestamp()), int(until.timestamp()), remote)
            followers = count(client.profile().get("followers_count"))
        keys = ("views", "reach", "total_interactions", "likes", "comments", "shares",
                "subscribersGained", "subscribersLost")
        self.db.metrics(account_id, f"period:{days}", day, {"sample": 1, **{key: counts.get(key) for key in keys}})
        self.db.metrics(account_id, "account", day, {"followers": followers})
        failures = []
        for post in self.db.query("""SELECT id,remote_id FROM publications
                                    WHERE account_id=? AND remote_id IS NOT NULL""", account_id):
            try:
                values = client.post_metrics(post["remote_id"], remote)
                self.db.metrics(account_id, f"post:{post['id']}", day, {"sample": 1, **values})
            except (ValueError, KeyError) as exc:
                failures.append(f"№{post['id']}: {self._safe_error(exc)}")
        return "Статистика обновлена." if not failures else "Часть данных недоступна. " + "; ".join(failures)


def reporting_zone(account):
    return ZoneInfo("America/Los_Angeles" if account["platform"] == "YouTube" else account["timezone"])


def report(db, account_id, days):
    account = db.account(account_id)
    today = datetime.now(reporting_zone(account)).date()
    period = f"period:{days}"
    sample = db.get("SELECT MAX(day) AS day FROM metrics WHERE account_id=? AND subject=? AND day<=?",
                    account_id, period, today.isoformat())["day"]
    day = date.fromisoformat(sample) if sample else today
    values = {row["name"]: row["value"] for row in db.query(
        "SELECT name,value FROM metrics WHERE account_id=? AND subject=? AND day=? AND name!='sample'",
        account_id, period, day.isoformat(),
    )}
    values["period_day"] = sample
    for shift, key in ((0, "followers"), (days, "baseline")):
        item = db.get("SELECT value FROM metrics WHERE account_id=? AND subject='account' AND name='followers' AND day=?",
                      account_id, (day - timedelta(days=shift)).isoformat())
        values[key] = item["value"] if item else None
    if "subscribersGained" in values and "subscribersLost" in values:
        values["followers_change"] = values["subscribersGained"] - values["subscribersLost"]
    elif account["platform"] == "Instagram" and values["followers"] is not None and values["baseline"] is not None:
        values["followers_change"] = values["followers"] - values["baseline"]
    subjects = db.query("""SELECT p.id,p.title,p.topic,m.name,m.value FROM publications p
                           LEFT JOIN metrics m ON m.account_id=p.account_id AND m.subject=('post:'||p.id)
                           AND m.day=(SELECT MAX(day) FROM metrics WHERE account_id=p.account_id
                                      AND subject=('post:'||p.id))
                           WHERE p.account_id=? AND p.remote_id IS NOT NULL ORDER BY p.due DESC""", account_id)
    posts = {}
    for row in subjects:
        entry = posts.setdefault(row["id"], {"title": row["title"], "topic": row["topic"] or account["topic"]})
        if row["name"] and row["name"] != "sample":
            entry[row["name"]] = row["value"]
    topics = {}
    for entry in posts.values():
        topic = entry["topic"] or "Без темы"
        group = topics.setdefault(topic, {"posts": 0, "coverage": 0, "views": 0})
        group["posts"] += 1
        if entry.get("views") is not None:
            group["coverage"] += 1
            group["views"] += entry["views"]
    return values, list(posts.values()), topics
