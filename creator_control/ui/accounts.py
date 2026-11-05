import time
import webbrowser
from datetime import date
from zoneinfo import available_timezones

from PySide6.QtCore import QDate
from PySide6.QtGui import QTextCharFormat
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QProgressBar, QSplitter, QVBoxLayout, QWidget

from ..planning import PHASES, STATUSES, US_ADVICE, preparation_status
from ..platforms import (
    connect_account, google_login, meta_login_finish, meta_login_url, refresh_instagram,
    token_key, Transport,
)
from ..publishing import Publisher
from ..tools import LDPlayer
from .common import Fields, Page, Table, button, calendar_widget, choices, label, required, scroll, surface, toolbar


def task_rows(rows):
    today = date.today().isoformat()
    for row in rows:
        if row["done"]:
            state = "Готово"
        elif row["due"] < today:
            state = "Просрочено"
        else:
            state = "Сегодня" if row["due"] == today else "Открыто"
        due = date.fromisoformat(row["due"]).strftime("%d.%m.%Y")
        yield row["id"], (due, state, row.get("account") or row["stage"], row["title"])


class Accounts(Page):
    def __init__(self, window):
        super().__init__(window, "Карточки собственных аккаунтов, аудитория и подключение официальных API.")
        self.editing = None
        self.callback = None
        self.table = Table(("Платформа", "Аккаунт", "API"), status=2, stretch=1,
                           empty="Создайте карточку первого аккаунта.")
        self.table.itemSelectionChanged.connect(self.guard(self.load))
        self.layout.addLayout(toolbar(button("Новый аккаунт", self.guard(self.new), True)))
        split = QSplitter()
        self.layout.addWidget(split, 1)
        split.addWidget(self.table)
        panel = QWidget()
        right = QVBoxLayout(panel)
        self.heading = label("Новый аккаунт", "section")
        right.addWidget(self.heading)
        self.form = Fields()
        right.addWidget(self.form)
        form = self.form
        form.add("platform", "Платформа", "choice", [(p, p) for p in ("Instagram", "YouTube")])
        form.add("name", "Название")
        form.add("profile", "Ссылка на профиль (HTTPS)")
        form.add("created", "Дата создания")
        form.add("kind", "Тип", "choice", [(p, p) for p in ("Новый", "Действующий")])
        form.add("topic", "Тематика")
        form.add("country", "Страна аудитории (US, DE и др.)").setMaxLength(2)
        form.add("language", "Язык контента")
        zone = form.add("timezone", "Часовой пояс аудитории", "choice",
                        [(z, z) for z in sorted(available_timezones())])
        zone.setMinimumContentsLength(15)
        form.add("subtopics", "Подтематики", "text")
        form.add("keywords", "Ключевые слова", "text")
        form.add("ideas", "Идеи и референсы", "text")
        form.add("start", "Начало календаря")
        form.add("duration", "Длительность, дней", "int", (3, 365))
        form.add("owner", "Ответственный")
        form.add("emulator", "Индекс LDPlayer")
        form.add("prep_override", "Статус подготовки", "choice", [("Автоматически", "")] + [(s, s) for s in STATUSES])
        self.advice = label(US_ADVICE, "muted")
        right.addWidget(self.advice)
        right.addLayout(toolbar(button("Сохранить", self.guard(self.save), True),
                                button("Открыть профиль", self.guard(self.profile)),
                                button("ИИ-план", self.guard(self.open_chat))))
        self.link = label("Сначала сохраните карточку.", "section")
        right.addWidget(self.link)
        right.addLayout(toolbar(button("Подключить OAuth", self.guard(self.authorize)),
                                button("Проверить", self.guard(self.verify)),
                                button("Продлить Instagram", self.guard(self.renew))))
        self.redirect = Fields()
        self.redirect.add("url", "Адрес после входа", "secret").setPlaceholderText("Вставьте полный redirect URL")
        right.addWidget(self.redirect)
        self.finish = button("Завершить Instagram OAuth", self.guard(self.complete_login))
        right.addWidget(self.finish)
        self.redirect.hide()
        self.finish.hide()
        right.addStretch(1)
        split.addWidget(scroll(panel))
        split.setSizes([310, 690])
        form.widget("country").textChanged.connect(self.audience)
        self.new()

    def audience(self, country):
        self.advice.setVisible(country.upper() == "US")

    def status(self, account):
        token = self.vault.get(token_key(account))
        if not account["remote_id"] or not token:
            return "Не подключён"
        return "Токен истёк" if token.get("expires", 0) <= time.time() else "Токен сохранён"

    def refresh(self):
        self.table.fill(((row["id"], (row["platform"], row["name"], self.status(row)))
                         for row in self.db.accounts()), self.editing)
        self.refresh_link()

    def refresh_link(self):
        if not self.editing:
            self.link.setText("Сначала сохраните карточку.")
        else:
            row = self.db.account(self.editing)
            self.link.setText(f"{self.status(row)} · {row['remote_name'] or 'OAuth'}")

    def clear_callback(self):
        self.callback = None
        self.redirect.widget("url").clear()
        self.redirect.hide()
        self.finish.hide()

    def new(self):
        self.editing = None
        self.clear_callback()
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        today = date.today().isoformat()
        self.form.set(dict(platform="Instagram", name="", profile="", created=today, start=today,
                           kind="Новый", topic="", country="US", language="English",
                           timezone="America/New_York", duration=14, owner="", emulator=None,
                           subtopics="", keywords="", ideas="", prep_override=""))
        self.form.widget("platform").setEnabled(True)
        self.heading.setText("Новый аккаунт")
        self.refresh_link()

    def load(self):
        identifier = self.table.selected()
        if identifier is None:
            return
        if self.editing != identifier:
            self.clear_callback()
        self.editing = identifier
        row = self.db.account(identifier)
        self.form.set({key: row[key] for key in self.form.fields})
        self.form.widget("platform").setEnabled(False)
        self.heading.setText(row["name"])
        self.refresh_link()

    def save(self):
        data = self.form.values()
        data["emulator"] = int(data["emulator"]) if data["emulator"] else None
        self.editing = self.db.save_account(data, self.editing)
        self.refresh()
        self.load()
        self.window.notice("Карточка сохранена. Календарь Instagram обновлён.")

    def selected(self):
        return self.db.account(required(self.editing, "сохранённый аккаунт"))

    def open_chat(self):
        self.window.open_account_chat(self.selected()["id"])

    def profile(self):
        account = self.selected()
        if not account["profile"]:
            raise ValueError("Добавьте ссылку в карточке.")
        webbrowser.open(account["profile"])

    def authorize(self):
        account = self.selected()
        platform = account["platform"]
        config = self.vault.get(f"config/{platform}") or {}
        self.clear_callback()
        if platform == "YouTube":
            http = Transport()
            self.window.job("Ожидание Google OAuth в браузере", lambda: connect_account(
                account, self.db, self.vault, google_login(config, http), http), self.connected)
        else:
            if not self.db.setting("meta_version"):
                raise ValueError("Сначала укажите версию Instagram Graph API в настройках.")
            url, state = meta_login_url(config)
            if not webbrowser.open(url):
                raise ValueError("Не удалось открыть браузер.")
            self.callback = (account, config, state, time.monotonic() + 600)
            self.redirect.show()
            self.finish.show()
            self.redirect.widget("url").setFocus()
            self.window.notice("Скопируйте полный адрес страницы после согласия в поле «Адрес после входа».")

    def complete_login(self):
        if not self.callback or time.monotonic() > self.callback[3]:
            self.clear_callback()
            raise ValueError("Начните OAuth заново.")
        account, config, state, _ = self.callback
        url = self.redirect.values()["url"]
        self.clear_callback()
        http = Transport()
        self.window.job("Проверка Instagram OAuth", lambda: connect_account(
            account, self.db, self.vault, meta_login_finish(config, url, state, http), http), self.connected)

    def connected(self, name):
        self.refresh()
        self.window.notice(f"Подключено: {name}")

    def verify(self):
        account = self.selected()
        self.window.job("Проверка доступа к API", lambda: Publisher(self.db, self.vault).client(account["id"]),
                        lambda _result: self.connected(account["name"]))

    def renew(self):
        account = self.selected()
        if account["platform"] != "Instagram":
            raise ValueError("Для YouTube токен обновляется автоматически.")
        self.window.job("Продление доступа Instagram", lambda: refresh_instagram(account, self.vault),
                        lambda _result: self.connected(account["name"]))


class Warmup(Page):
    def __init__(self, window):
        super().__init__(window, "Чек-лист подготовки профиля, исследования и первых публикаций.")
        self.rows, self.marked = [], []
        self.current_account = None
        self.phase = None
        self.account = QComboBox()
        self.account.setAccessibleName("Instagram-аккаунт")
        self.account.setPlaceholderText("Нет Instagram-аккаунтов")
        self.account.setMinimumWidth(215)
        self.account.currentIndexChanged.connect(self.guard(self.show_calendar))
        self.ld = button("Открыть в LDPlayer", self.guard(self.open_app))
        self.ai = button("ИИ-план", self.guard(lambda: self.window.open_account_chat(
            required(self.account.currentData(), "аккаунт"))))
        self.add_account = button("Добавить аккаунт", lambda: self.window.navigate("accounts"), True)
        self.layout.addLayout(toolbar(self.account, self.ld, self.ai, self.add_account))
        progress_card, progress_box = surface()
        self.layout.addWidget(progress_card)
        heading = QHBoxLayout()
        self.status = label("", "section")
        heading.addWidget(self.status, 1)
        self.progress_text = label("", "muted")
        heading.addWidget(self.progress_text)
        progress_box.addLayout(heading)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Доля завершённых задач")
        progress_box.addWidget(self.progress)
        phases = QHBoxLayout()
        phases.setSpacing(8)
        self.phase_buttons = {}
        for index, ((key, title, _), short) in enumerate(zip(PHASES, (
                "Настройка", "Исследование", "Контент", "Публикации")), 1):
            item = button("", lambda _checked, phase=key: self.select_phase(phase))
            item.setObjectName("phase")
            item.setCheckable(True)
            item.setAccessibleName(title)
            item.setToolTip(f"Показать задачи этапа «{title}»")
            self.phase_buttons[key] = (item, f"{index:02}  {short}")
            phases.addWidget(item, 1)
        self.layout.addLayout(phases)
        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.layout.addWidget(split, 1)
        calendar_panel, calendar_box = surface()
        cal_header = QHBoxLayout()
        cal_header.addWidget(label("Календарь", "section"), 1)
        cal_header.addWidget(button("Сегодня", self.guard(self.today)))
        calendar_box.addLayout(cal_header)
        self.calendar = calendar_widget()
        self.calendar.setMinimumHeight(172)
        self.calendar.selectionChanged.connect(self.guard(self.show_tasks))
        calendar_box.addWidget(self.calendar)
        self.day_only = QCheckBox("Только выбранный день")
        self.day_only.toggled.connect(self.guard(self.show_tasks))
        calendar_box.addWidget(self.day_only)
        self.advice = label("", "muted")
        self.advice.setToolTip(US_ADVICE)
        calendar_box.addWidget(self.advice)
        calendar_box.addStretch(1)
        split.addWidget(calendar_panel)
        task_panel = QWidget()
        task_box = QVBoxLayout(task_panel)
        task_box.setContentsMargins(0, 0, 0, 0)
        task_box.setSpacing(8)
        task_heading = QHBoxLayout()
        self.list_title = label("Задачи", "section")
        task_heading.addWidget(self.list_title, 1)
        self.all_phases = button("Все этапы", self.guard(lambda: self.select_phase(None)))
        task_heading.addWidget(self.all_phases)
        task_box.addLayout(task_heading)
        self.table = Table(("Срок", "Состояние", "Задача"), status=1)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setDefaultSectionSize(40)
        task_box.addWidget(self.table, 1)
        self.toggle_button = button("Отметить выполненной", self.guard(self.toggle), True)
        task_box.addLayout(toolbar(self.toggle_button))
        self.table.itemSelectionChanged.connect(self.update_toggle)
        split.addWidget(task_panel)
        split.setSizes([280, 680])

    def refresh(self):
        choices(self.account, ((row["name"], row["id"]) for row in self.db.accounts() if row["platform"] == "Instagram"))
        self.show_calendar()

    def today(self):
        self.day_only.setChecked(True)
        self.calendar.setSelectedDate(QDate.currentDate())

    def select_phase(self, phase):
        self.phase = None if self.phase == phase else phase
        self.show_tasks()

    def show_calendar(self):
        identifier = self.account.currentData()
        self.rows = self.db.tasks(identifier) if identifier is not None else []
        account = self.db.account(identifier) if identifier is not None else None
        if identifier != self.current_account:
            self.current_account = identifier
            self.phase = None
            self.table.clearSelection()
        self.add_account.setVisible(account is None)
        self.account.setEnabled(account is not None)
        self.ai.setEnabled(account is not None)
        self.ld.setVisible(account is not None)
        self.ld.setEnabled(bool(account and account["emulator"] is not None and self.db.setting("ldplayer")))
        self.ld.setToolTip("Укажите папку LDPlayer в настройках и его индекс в карточке аккаунта.")
        for day in self.marked:
            self.calendar.setDateTextFormat(day, QTextCharFormat())
        self.marked.clear()
        if account is None:
            self.status.setText("Создайте карточку Instagram")
        else:
            active = self.db.get("SELECT id FROM publications WHERE account_id=? AND state='confirmed'", identifier)
            status = preparation_status(account, self.rows)
            self.status.setText("Публикуется" if active and not account["prep_override"] else status)
        self.advice.setText("США · Английский, субтитры, местный контекст и часовой пояс."
                            if account and account["country"] == "US" else "")
        self.advice.setVisible(bool(self.advice.text()))
        total = len(self.rows)
        completed = sum(row["done"] for row in self.rows)
        percent = round(100 * completed / total) if total else 0
        self.progress.setValue(percent)
        self.progress_text.setText(f"{percent}% · {completed} из {total} задач" if total else "")
        self.progress.setVisible(bool(total))
        highlighted = QTextCharFormat()
        highlighted.setFontWeight(700)
        for due in {row["due"] for row in self.rows}:
            day = QDate.fromString(due, "yyyy-MM-dd")
            self.calendar.setDateTextFormat(day, highlighted)
            self.marked.append(day)
        self.show_tasks()

    def show_tasks(self):
        day = self.calendar.selectedDate().toString("yyyy-MM-dd")
        for key, (item, title) in self.phase_buttons.items():
            rows = [row for row in self.rows if row["phase"] == key]
            count = f"{sum(row['done'] for row in rows)} из {len(rows)} задач" if rows else "—"
            item.setText(f"{title}\n{count}")
            item.setChecked(key == self.phase)
            item.setEnabled(bool(rows))
        visible = [row for row in self.rows if (not self.phase or row["phase"] == self.phase)
                   and (not self.day_only.isChecked() or row["due"] == day)]
        self.list_title.setText(f"Задачи · {len(visible)}")
        self.all_phases.setEnabled(self.phase is not None)
        self.table.empty_text("После добавления аккаунта здесь появится план." if not self.rows
                              else "По выбранным фильтрам задач нет.")
        self.table.fill((identifier, (due, state, title))
                        for identifier, (due, state, _stage, title) in task_rows(visible))
        self.update_toggle()

    def update_toggle(self):
        selected = next((row for row in self.rows if row["id"] == self.table.selected()), None)
        self.toggle_button.setEnabled(selected is not None)
        self.toggle_button.setText("Вернуть в задачи" if selected and selected["done"] else "Отметить выполненной")

    def toggle(self):
        task_id = required(self.table.selected(), "задачу")
        row = next(row for row in self.rows if row["id"] == task_id)
        self.db.complete(task_id, not row["done"])
        self.show_calendar()

    def open_app(self):
        account = self.db.account(required(self.account.currentData(), "Instagram-аккаунт"))
        index = account["emulator"]
        if index is None:
            raise ValueError("Укажите индекс LDPlayer в карточке аккаунта.")
        directory = self.db.setting("ldplayer")
        self.window.job("Открытие Instagram", lambda: LDPlayer(directory).action(index, "instagram"),
                        lambda _result: self.window.notice("Команда открытия Instagram отправлена."))
