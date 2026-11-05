import re
import webbrowser
from datetime import date
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QTabWidget, QVBoxLayout, QWidget

from ..db import https_url
from ..chat import DEFAULT_MODEL, KEY_NAME
from ..tools import network_check
from .common import Fields, Page, button, label, scroll, toolbar


class Settings(Page):
    def __init__(self, window):
        super().__init__(window, "Пути, внешний вид, тестовый режим, OAuth и безопасность подключения.")
        self.tabs = QTabWidget()
        self.layout.addWidget(self.tabs, 1)
        panel = QWidget()
        general = QVBoxLayout(panel)
        self.form = Fields()
        form = self.form
        form.add("theme", "Тема", "choice", [("Тёмная", "dark"), ("Светлая", "light")])
        form.add("test_mode", "Тестовый режим", "bool")
        form.add("ldplayer", "Папка LDPlayer")
        general.addWidget(form)
        general.addLayout(toolbar(button("Выбрать LDPlayer…", self.guard(lambda: self.directory("ldplayer")))))
        self.ffmpeg = Fields()
        self.ffmpeg.add("ffmpeg", "Папка FFmpeg / ffprobe")
        general.addWidget(self.ffmpeg)
        general.addLayout(toolbar(button("Выбрать FFmpeg…", self.guard(lambda: self.directory("ffmpeg")))))
        general.addWidget(label("При пустом пути используется локальный FFmpeg проекта или PATH. "
                                "Тестовый режим проверяет черновик локально.", "muted"))
        general.addLayout(toolbar(button("Сохранить настройки", self.guard(self.save), True),
                                  button("Резервная копия SQLite…", self.guard(self.backup))))
        general.addWidget(label(str(self.db.path), "muted"))
        general.addStretch(1)
        self.tabs.addTab(scroll(panel), "Программа")
        panel = QWidget()
        oauth = QVBoxLayout(panel)
        self.configs = {}
        for platform in ("YouTube", "Instagram"):
            oauth.addWidget(label(platform, "section"))
            config = Fields()
            config.add("client_id", "Client ID" if platform == "YouTube" else "Instagram App ID")
            config.add("secret", "Secret", "secret").setPlaceholderText("Пустое поле сохраняет текущий секрет")
            if platform == "Instagram":
                config.add("redirect", "Зарегистрированный HTTPS redirect URI")
                config.add("meta_version", "Версия Instagram Graph API").setPlaceholderText("Например, v25.0")
            oauth.addWidget(config)
            oauth.addLayout(toolbar(button("Сохранить " + platform, self.guard(lambda p=platform: self.save_oauth(p)), True)))
            self.configs[platform] = config
        oauth.addWidget(label("Секреты и токены хранятся в Windows Credential Manager.", "muted"))
        oauth.addStretch(1)
        self.tabs.addTab(scroll(panel), "OAuth")
        panel = QWidget()
        security = QVBoxLayout(panel)
        self.network = Fields()
        self.network.add("platform", "Сервис", "choice", [(p, p) for p in ("YouTube", "Instagram")])
        self.network.add("proxy", "HTTP(S)-прокси", "secret").setPlaceholderText("URL, при необходимости с логином и паролем")
        self.network.add("external", "Проверить внешний IP через ipify.org", "bool")
        self.network.add("use_proxy", "Использовать сохранённый прокси для теста", "bool")
        security.addWidget(self.network)
        security.addLayout(toolbar(button("Сохранить прокси", self.guard(self.save_proxy)),
                                   button("Удалить прокси", self.guard(self.clear_proxy))))
        security.addLayout(toolbar(button("Проверить соединение", self.guard(self.diagnose), True)))
        self.net_result = label("Три запроса к выбранному сервису измеряют доступность и задержку.", "section")
        security.addWidget(self.net_result)
        security.addWidget(label("IP сравнивается только в текущем сеансе. Прокси используется для этой диагностики.", "muted"))
        security.addStretch(1)
        self.tabs.addTab(scroll(panel), "Безопасность подключения")
        self.previous_ip = {}
        panel = QWidget()
        chat_box = QVBoxLayout(panel)
        chat_box.addWidget(label("Подключение ИИ-чата", "section"))
        chat_box.addWidget(label("Запросы отправляются в OpenAI и используют баланс вашего API-проекта. "
                                 "В новой беседе можно явно выбрать контекст аккаунта.", "muted"))
        self.chat_form = Fields()
        self.chat_form.add("key", "OpenAI API-ключ", "secret").setPlaceholderText("Пустое поле сохраняет текущий ключ")
        self.chat_form.add("model", "Модель").setPlaceholderText(DEFAULT_MODEL)
        self.chat_form.add("limit", "Лимит токенов ответа", "int", (256, 16000))
        chat_box.addWidget(self.chat_form)
        self.chat_status = label("", "muted")
        chat_box.addWidget(self.chat_status)
        chat_box.addLayout(toolbar(button("Сохранить", self.guard(self.save_chat), True),
                                   button("Удалить ключ", self.guard(self.remove_chat_key))))
        chat_box.addLayout(toolbar(button("Открыть страницу API-ключей",
                                         lambda: webbrowser.open("https://platform.openai.com/api-keys"))))
        chat_box.addWidget(label("История чатов хранится в локальной базе, ключ — в Windows Credential Manager.",
                                 "muted"))
        chat_box.addStretch(1)
        self.tabs.addTab(scroll(panel), "ИИ-чат")

    def refresh(self):
        self.form.set({key: self.db.setting(key) for key in ("ldplayer",)})
        self.form.set({"theme": self.db.setting("theme", "dark"), "test_mode": self.db.setting("test_mode", "1") != "0"})
        self.ffmpeg.set({"ffmpeg": self.db.setting("ffmpeg")})
        for platform, form in self.configs.items():
            config = self.vault.get(f"config/{platform}") or {}
            values = {"client_id": config.get("client_id", ""), "secret": ""}
            if platform == "Instagram":
                values.update(redirect=config.get("redirect", ""), meta_version=self.db.setting("meta_version"))
            form.set(values)
        self.chat_form.set({"key": "", "model": self.db.setting("chat_model", DEFAULT_MODEL),
                            "limit": int(self.db.setting("chat_output_limit", "4096"))})
        self.chat_status.setText("API-ключ сохранён." if self.vault.get(KEY_NAME) else "API-ключ пока не добавлен.")

    def save_chat(self):
        data = self.chat_form.values()
        model = data["model"].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}", model):
            raise ValueError("Укажите корректное имя модели.")
        key = data["key"]
        if key and (len(key) < 20 or any(c.isspace() for c in key)):
            raise ValueError("Проверьте API-ключ. Он должен быть без пробелов и переносов строки.")
        if key:
            self.vault.put(KEY_NAME, key)
        self.db.settings({"chat_model": model, "chat_output_limit": str(data["limit"])})
        self.chat_form.widget("key").clear()
        self.chat_status.setText("API-ключ сохранён." if self.vault.get(KEY_NAME) else "API-ключ пока не добавлен.")
        self.window.notice("Настройки ИИ-чата сохранены.")

    def remove_chat_key(self):
        if self.window.confirm("Удалить API-ключ", "Удалить сохранённый OpenAI API-ключ с этого компьютера?"):
            self.vault.remove(KEY_NAME)
            self.chat_status.setText("API-ключ удалён.")
            self.window.notice("Ключ удалён из Windows Credential Manager.")

    def directory(self, key):
        fields = self.ffmpeg if key == "ffmpeg" else self.form
        initial = fields.values()[key] or str(self.db.path.parent.parent)
        result = QFileDialog.getExistingDirectory(self, "Выберите папку", initial)
        if result:
            fields.set({key: result})

    def save(self):
        data = {**self.form.values(), **self.ffmpeg.values()}
        if data["ffmpeg"] and not all((Path(data["ffmpeg"]) / exe).is_file() for exe in ("ffmpeg.exe", "ffprobe.exe")):
            raise ValueError("В папке FFmpeg должны быть ffmpeg.exe и ffprobe.exe.")
        if data["ldplayer"] and not any((Path(data["ldplayer"]) / exe).is_file()
                                       for exe in ("ldconsole.exe", "dnconsole.exe")):
            raise ValueError("В папке LDPlayer должен быть ldconsole.exe или dnconsole.exe.")
        if not data["test_mode"] and self.db.setting("test_mode", "1") != "0":
            if not self.window.confirm("Реальные операции API",
                                       "Выключить тестовый режим? Подтверждённая отправка сможет создать публикацию."):
                self.form.set({"test_mode": True})
                return
        data["test_mode"] = "1" if data["test_mode"] else "0"
        self.db.settings(data)
        self.window.theme()
        self.window.notice("Настройки сохранены.")

    def save_oauth(self, platform):
        form = self.configs[platform]
        data = form.values()
        old = self.vault.get(f"config/{platform}") or {}
        data["secret"] = data["secret"] or old.get("secret", "")
        if not data["client_id"] or not data["secret"]:
            raise ValueError("Укажите ID и Secret приложения.")
        if platform == "Instagram":
            version = data.pop("meta_version")
            if not re.fullmatch(r"v[1-9][0-9]*\.0", version):
                raise ValueError("Укажите поддерживаемую версию Instagram Graph API.")
            uri = https_url(data["redirect"])
            if uri.query or uri.fragment:
                raise ValueError("Зарегистрированный redirect URI должен быть без параметров и фрагмента.")
        self.vault.put(f"config/{platform}", data)
        if platform == "Instagram":
            self.db.settings({"meta_version": version})
        form.widget("secret").clear()
        self.window.notice("Конфигурация OAuth сохранена.")

    def backup(self):
        file, _ = QFileDialog.getSaveFileName(self, "Резервная копия SQLite",
                                             str(self.window.data_folder("backups") / f"ccc-{date.today().isoformat()}.sqlite3"),
                                             "SQLite (*.sqlite3)")
        if file:
            self.window.job("Резервное копирование SQLite", lambda: self.db.backup(file),
                            lambda result: self.window.notice(f"Копия проверена: {result}"))

    def save_proxy(self):
        from urllib.parse import urlsplit
        value = self.network.values()["proxy"]
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port or
                parsed.query or parsed.fragment or parsed.path not in ("", "/") or any(c.isspace() for c in value)):
            raise ValueError("Укажите URL HTTP(S)-прокси с портом.")
        self.vault.put("network/proxy", value)
        self.network.widget("proxy").clear()
        self.previous_ip.pop(True, None)
        self.window.notice("Прокси сохранён в Windows Credential Manager.")

    def clear_proxy(self):
        if self.window.confirm("Удалить прокси", "Удалить сохранённую конфигурацию диагностики?"):
            self.vault.remove("network/proxy")
            self.previous_ip.pop(True, None)
            self.window.notice("Конфигурация удалена.")

    def diagnose(self):
        data = self.network.values()
        use_proxy = data["use_proxy"]
        proxy = self.vault.get("network/proxy") if use_proxy else None
        if use_proxy and not proxy:
            raise ValueError("Сначала сохраните прокси.")
        self.window.job("Проверка безопасности подключения",
                        lambda: network_check(data["platform"], proxy, data["external"]),
                        lambda result: self.show_net(result, use_proxy, data["external"]))

    def show_net(self, result, proxied, external):
        times, ip = result
        successes = sum(value is not None for value in times)
        values = " / ".join(f"{value} мс" if value is not None else "сбой" for value in times)
        status = "Доступно" if successes == 3 else "Нестабильно" if successes else "Подключение потеряно"
        text = f"{status} · {values}"
        if ip:
            previous = self.previous_ip.get(proxied)
            text += (" · внешний IP изменился" if previous and previous != ip else
                     " · внешний IP без изменений" if previous else " · исходный внешний IP определён")
            self.previous_ip[proxied] = ip
        elif external:
            text += " · внешний IP недоступен"
        self.net_result.setText(text)
