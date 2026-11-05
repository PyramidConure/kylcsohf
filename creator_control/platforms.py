import base64
import hashlib
import re
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .db import https_url

GOOGLE = "https://www.googleapis.com"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE = GOOGLE + "/youtube/v3"
META = "https://graph.instagram.com"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
META_SCOPES = ("instagram_business_basic", "instagram_business_content_publish",
               "instagram_business_manage_insights")


class APIError(ValueError):
    def __init__(self, service, status=None, code=None):
        self.status, self.code = status, code
        detail = f"HTTP {status}" if status else "сбой соединения"
        if code is not None:
            detail += f", код {code}"
        if status in (401, 403) or code in (190, 102):
            detail += ". Проверьте авторизацию, разрешения и квоты приложения"
        super().__init__(f"{service}: {detail}.")


def _json(response):
    try:
        result = response.json()
        return result if isinstance(result, dict) else {}
    except ValueError:
        return {}


def _safe_code(response):
    error = _json(response).get("error")
    if isinstance(error, dict):
        items = error.get("errors")
        reason = (items[0].get("reason") if isinstance(items, list) and items and isinstance(items[0], dict)
                  else error.get("code"))
    else:
        reason = error
    return reason if re.fullmatch(r"[A-Za-z0-9_]{1,48}", str(reason or "")) else None


class Transport:
    def send(self, method, url, *, service, accepted=(), **kwargs):
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(method, url, timeout=(8, 60), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise APIError(service) from None
        if not 200 <= response.status_code < 300 and response.status_code not in accepted:
            raise APIError(service, response.status_code, _safe_code(response))
        return response

    def json(self, method, url, *, service, **kwargs):
        response = self.send(method, url, service=service, **kwargs)
        data = _json(response)
        if not data or "error" in data:
            raise APIError(service, response.status_code, _safe_code(response))
        return data


def _remember_token(data, refresh=None):
    if not data.get("access_token"):
        raise ValueError("Сервис не вернул токен.")
    return {"access": data["access_token"], "refresh": data.get("refresh_token", refresh),
            "expires": time.time() + int(data.get("expires_in") or 3600)}


def google_login(config, http, open_browser=webbrowser.open):
    if not config.get("client_id") or not config.get("secret"):
        raise ValueError("Укажите Desktop Client ID и Client Secret Google.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urlsplit(self.path)
            values = parse_qs(query.query)
            if (query.path != "/oauth" or
                    not secrets.compare_digest(values.get("state", [""])[0], state)):
                self.send_error(400, "Invalid callback")
                return
            callback.update(values)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write("Ответ принят. Вернитесь в Creator Control Center.".encode())

        def log_message(self, *_args):
            pass

        def setup(self):
            self.request.settimeout(4)
            super().setup()

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.5
        redirect = f"http://127.0.0.1:{server.server_port}/oauth"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        if not open_browser(url):
            raise ValueError("Не удалось открыть браузер.")
        end = time.monotonic() + 120
        while not callback and time.monotonic() < end:
            server.handle_request()
    if not callback.get("code") or callback.get("error"):
        raise ValueError("Google OAuth отменён или истекло время ожидания.")
    return _remember_token(http.json("POST", GOOGLE_TOKEN, service="Google OAuth", data={
        "client_id": config["client_id"], "client_secret": config["secret"],
        "code": callback["code"][0], "code_verifier": verifier, "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }))


def meta_login_url(config):
    if not all(config.get(k) for k in ("client_id", "secret", "redirect")):
        raise ValueError("Заполните Instagram App ID, App Secret и redirect URI.")
    parsed = https_url(config["redirect"])
    if parsed.query or parsed.fragment:
        raise ValueError("Redirect URI должен быть HTTPS без параметров или фрагмента.")
    state = secrets.token_urlsafe(32)
    url = "https://www.instagram.com/oauth/authorize?" + urlencode({
        "enable_fb_login": 0, "force_authentication": 1, "client_id": config["client_id"],
        "redirect_uri": config["redirect"], "response_type": "code", "scope": ",".join(META_SCOPES),
        "state": state,
    })
    return url, state


def meta_login_finish(config, callback, state, http):
    actual = https_url(callback)
    expected = https_url(config["redirect"])
    params = parse_qs(actual.query)
    if ((actual.netloc, actual.path) != (expected.netloc, expected.path) or
            not secrets.compare_digest(params.get("state", [""])[0], state) or not params.get("code") or
            params.get("error")):
        raise ValueError("Redirect URI или состояние OAuth не совпадают, либо доступ отклонён.")
    short = _remember_token(http.json("POST", "https://api.instagram.com/oauth/access_token",
                                      service="Instagram OAuth", data={
        "client_id": config["client_id"], "client_secret": config["secret"],
        "redirect_uri": config["redirect"], "grant_type": "authorization_code", "code": params["code"][0],
    }))
    long = http.json("GET", META + "/access_token", service="Instagram OAuth", params={
        "grant_type": "ig_exchange_token", "client_secret": config["secret"], "access_token": short["access"],
    })
    return _remember_token(long)


def token_key(account):
    return f"oauth/{account['id']}"


def api_for(account, db, vault, http=None, token=None):
    http = http or Transport()
    platform = account["platform"]
    token = token or vault.get(token_key(account))
    if not token:
        raise ValueError("Подключите аккаунт через OAuth.")
    if token.get("expires", 0) <= time.time() + 60:
        if platform == "Instagram":
            raise ValueError("Токен Instagram истёк или истекает. Подключите аккаунт заново.")
        config = vault.get("config/YouTube") or {}
        if not token.get("refresh") or not config.get("client_id") or not config.get("secret"):
            raise ValueError("Обновите конфигурацию Google OAuth и переподключите канал.")
        token = _remember_token(http.json("POST", GOOGLE_TOKEN, service="Google OAuth", data={
            "client_id": config["client_id"], "client_secret": config["secret"],
            "refresh_token": token["refresh"], "grant_type": "refresh_token",
        }), token["refresh"])
        vault.put(token_key(account), token)
    if platform == "YouTube":
        return YouTube(http, token["access"])
    return Instagram(http, token["access"], db.setting("meta_version"))


def connect_account(account, db, vault, token, http=None):
    client = api_for(account, db, vault, http, token)
    profile, name = client.identity()
    db.bind(account["id"], profile, name)
    vault.put(token_key(account), token)
    return name


def refresh_instagram(account, vault, http=None):
    http = http or Transport()
    key = token_key(account)
    token = vault.get(key)
    if not token or token.get("expires", 0) <= time.time():
        raise ValueError("Сначала заново подключите Instagram.")
    data = http.json("GET", META + "/refresh_access_token", service="Instagram OAuth", params={
        "grant_type": "ig_refresh_token", "access_token": token["access"],
    })
    vault.put(key, _remember_token(data))


def count(value):
    if type(value) is int or (isinstance(value, str) and value.isascii() and value.isdecimal()):
        return int(value) if 0 <= int(value) < 2**63 else None
    return None


def valid_id(value, platform):
    pattern = r"[0-9]{1,32}" if platform == "Instagram" else r"[A-Za-z0-9_-]{1,64}"
    if not re.fullmatch(pattern, str(value or "")):
        raise ValueError("Платформа вернула некорректный идентификатор.")
    return str(value)


class YouTube:
    def __init__(self, http, token):
        self.http = http
        self.headers = {"Authorization": "Bearer " + token}

    def call(self, endpoint, **params):
        return self.http.json("GET", YOUTUBE + endpoint, service="YouTube",
                              headers=self.headers, params=params)

    def channel(self):
        items = self.call("/channels", part="snippet,statistics", mine="true").get("items", [])
        if len(items) != 1:
            raise ValueError("OAuth должен вернуть ровно один доступный YouTube-канал.")
        return items[0]

    def identity(self):
        channel = self.channel()
        return valid_id(channel.get("id"), "YouTube"), channel["snippet"]["title"]

    def start_upload(self, post, size):
        response = self.http.send("POST", GOOGLE + "/upload/youtube/v3/videos", service="YouTube",
            params={"part": "snippet,status", "uploadType": "resumable"},
            headers={**self.headers, "X-Upload-Content-Length": str(size),
                     "X-Upload-Content-Type": "application/octet-stream"}, json={
                "snippet": {"title": post["title"], "description": post["caption"], "categoryId": "22"},
                "status": {"privacyStatus": post["privacy"],
                           "selfDeclaredMadeForKids": bool(post["for_kids"]),
                           "containsSyntheticMedia": bool(post["synthetic"])},
            })
        return self._session_url(response.headers.get("Location", ""))

    @staticmethod
    def _session_url(url):
        parsed = https_url(url, {"www.googleapis.com"})
        if parsed.path != "/upload/youtube/v3/videos" or parsed.port not in (None, 443):
            raise ValueError("Неожиданный адрес сессии загрузки Google.")
        return url

    def upload_state(self, session, size, offset=None, chunk=None):
        last = offset + len(chunk) - 1 if chunk else None
        span = f"bytes {offset}-{last}/{size}" if chunk else f"bytes */{size}"
        response = self.http.send("PUT", self._session_url(session), service="YouTube",
                                  accepted=(308,), data=chunk or b"",
                                  headers={**self.headers, "Content-Range": span,
                                           "Content-Type": "application/octet-stream"})
        if response.status_code != 308:
            return size, valid_id(_json(response).get("id"), "YouTube")
        received = response.headers.get("Range", "")
        match = re.fullmatch(r"bytes=0-([0-9]+)", received)
        next_offset = int(match[1]) + 1 if match else 0
        if (received and not match) or next_offset >= size:
            raise ValueError("YouTube вернул некорректное подтверждение диапазона.")
        return next_offset, None

    def video(self, video_id, expected_account):
        items = self.call("/videos", id=valid_id(video_id, "YouTube"),
                          part="snippet,statistics,status,processingDetails").get("items", [])
        if len(items) != 1 or items[0].get("snippet", {}).get("channelId") != expected_account:
            raise ValueError("YouTube не вернул видео выбранного канала.")
        return items[0]

    def account_metrics(self, start, end):
        metrics = ("views", "likes", "comments", "shares", "subscribersGained", "subscribersLost")
        data = self.http.json("GET", "https://youtubeanalytics.googleapis.com/v2/reports",
                              service="YouTube Analytics", headers=self.headers, params={
            "ids": "channel==MINE", "startDate": start, "endDate": end, "metrics": ",".join(metrics),
        })
        rows = data.get("rows", [])
        return {key: count(value) for key, value in zip(metrics, rows[0])} if rows else {}

    def post_metrics(self, video_id, expected_account):
        stats = self.video(video_id, expected_account).get("statistics", {})
        return {key: count(stats.get(name)) for key, name in
                (("views", "viewCount"), ("likes", "likeCount"), ("comments", "commentCount"))}


class Instagram:
    def __init__(self, http, token, version):
        if not re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", version):
            raise ValueError("Укажите поддерживаемую версию Instagram Graph API в настройках (vNN.0).")
        self.http = http
        self.base = META + "/" + version
        self.headers = {"Authorization": "Bearer " + token}

    def call(self, method, path, **kwargs):
        return self.http.json(method, self.base + path, service="Instagram", headers=self.headers, **kwargs)

    def profile(self):
        return self.call("GET", "/me", params={"fields": "user_id,username,followers_count"})

    def identity(self):
        profile = self.profile()
        return valid_id(profile.get("user_id"), "Instagram"), profile["username"]

    def create_reel(self, account_id, video_url, caption):
        result = self.call("POST", f"/{account_id}/media", data={
            "media_type": "REELS", "video_url": video_url, "caption": caption,
        })
        return valid_id(result.get("id"), "Instagram")

    def container_status(self, container):
        return self.call("GET", f"/{container}", params={"fields": "status_code"}).get("status_code")

    def reel(self, media_id):
        media_id = valid_id(media_id, "Instagram")
        data = self.call("GET", f"/{media_id}", params={"fields": "id,media_product_type"})
        if str(data.get("id")) != media_id or data.get("media_product_type") != "REELS":
            raise ValueError("Meta не подтвердила публикацию с этим ID.")

    def publish_reel(self, account_id, container):
        result = self.call("POST", f"/{account_id}/media_publish", data={"creation_id": container})
        return valid_id(result.get("id"), "Instagram")

    def insights(self, subject, names, **params):
        results = {}
        for name in names:
            try:
                data = self.call("GET", f"/{subject}/insights", params={"metric": name, **params})
            except APIError as error:
                if error.status != 400 or str(error.code) not in ("100", "10"):
                    raise
                results[name] = None
                continue
            row = next((row for row in data.get("data", []) if row.get("name") == name), {})
            values = row.get("values") or [{}]
            results[name] = count(row.get("total_value", {}).get("value", values[0].get("value")))
        return results

    def account_metrics(self, start_epoch, end_epoch, account):
        return self.insights(account, ("views", "reach", "total_interactions"), period="day",
                             metric_type="total_value", since=start_epoch, until=end_epoch)

    def post_metrics(self, video_id, _expected_account):
        return self.insights(valid_id(video_id, "Instagram"),
                             ("views", "reach", "total_interactions", "likes", "comments", "shares", "saved"))
