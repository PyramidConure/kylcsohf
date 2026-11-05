from datetime import UTC, date, datetime, timedelta

from PySide6.QtCore import QDate
from PySide6.QtGui import QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from ..db import report_period
from ..publishing import LABELS, METRICS, Publisher, report, reporting_zone
from .common import Fields, Page, Table, button, calendar_widget, choices, label, required, scroll, surface, toolbar


def local_time(value):
    return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")


class Posts(Page):
    def __init__(self, window):
        super().__init__(window, "Календарь показывает местное время Windows. Отправка начинается по подтверждению, когда наступит срок.")
        self.editing = None
        self.saved_form = None
        self.video_defaults = {}
        self.marked = []
        self.publisher = Publisher(self.db, self.vault)
        self.send_button = button("Проверить черновик", self.guard(lambda: self.send(False)))
        self.resume_button = button("Продолжить отправку", self.guard(lambda: self.send(True)))
        self.check_button = button("Проверить результат", self.guard(self.check))
        self.layout.addLayout(toolbar(button("Новый черновик", self.guard(self.new), True)))
        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.layout.addWidget(split, 1)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(9)
        self.queue_title = label("Очередь", "section")
        left_box.addWidget(self.queue_title)
        self.table = Table(("Срок", "Публикация", "Состояние"), status=2, stretch=1)
        self.table.itemSelectionChanged.connect(self.guard(self.load))
        left_box.addWidget(self.table, 1)
        calendar_panel, calendar_box = surface()
        cal_header = QHBoxLayout()
        cal_header.addWidget(label("Календарь", "section"), 1)
        cal_header.addWidget(button("Сегодня", self.guard(self.today)))
        calendar_box.addLayout(cal_header)
        self.calendar = calendar_widget()
        self.calendar.selectionChanged.connect(self.guard(self.refresh))
        self.calendar.setMaximumHeight(190)
        calendar_box.addWidget(self.calendar)
        self.day_only = QCheckBox("Только выбранный день")
        self.day_only.toggled.connect(self.guard(self.refresh))
        calendar_box.addWidget(self.day_only)
        left_box.addWidget(calendar_panel)
        split.addWidget(left)
        panel, right = surface()
        self.heading = label("Новый черновик", "section")
        right.addWidget(self.heading)
        self.platform_hint = label("", "muted")
        right.addWidget(self.platform_hint)
        self.form = Fields()
        right.addWidget(self.form)
        form = self.form
        form.add("account_id", "Аккаунт", "choice").currentIndexChanged.connect(self.guard(self.platform_fields))
        form.add("video_id", "Видео", "choice").currentIndexChanged.connect(self.guard(self.fill_title))
        form.add("title", "Название")
        form.add("caption", "Описание", "text")
        form.add("topic", "Тема публикации")
        form.add("due", "Дата и время", "datetime").setToolTip("Местное время Windows")
        form.add("privacy", "Видимость", "choice", [
            ("Приватно", "private"), ("Публично", "public"), ("По ссылке", "unlisted"),
        ])
        form.add("for_kids", "Контент для детей", "bool")
        form.add("synthetic", "Синтетический контент", "bool")
        form.add("video_url", "HTTPS-ссылка на видео").setPlaceholderText("https://…")
        self.save_button = button("Сохранить", self.guard(self.save), True)
        self.save_button.setToolTip("Сохранить черновик в очередь")
        right.addLayout(toolbar(self.save_button, self.send_button))
        right.addLayout(toolbar(self.resume_button, self.check_button))
        self.result = label("", "muted")
        right.addWidget(self.result)
        self.link = Fields()
        self.link.add("remote", "ID публикации после сверки на платформе")
        right.addWidget(self.link)
        self.link_button = button("Связать ID публикации", self.guard(self.associate))
        right.addLayout(toolbar(self.link_button))
        self.reset = button("Вернуть отклонённый Reels в очередь", self.guard(self.retry_container))
        right.addWidget(self.reset)
        self.release = button("Вернуть проверенный черновик в очередь", self.guard(self.release_draft))
        right.addWidget(self.release)
        self.log = Table(("Дата", "Событие"))
        self.log.setMinimumHeight(142)
        self.log_title = label("Журнал операции", "section")
        right.addWidget(self.log_title)
        right.addWidget(self.log)
        right.addStretch(1)
        split.addWidget(scroll(panel))
        split.setSizes([545, 455])
        self.new()

    def today(self):
        self.calendar.setSelectedDate(QDate.currentDate())
        self.day_only.setChecked(True)

    def platform_fields(self):
        identifier = self.form.widget("account_id").currentData()
        account = self.db.account(identifier) if identifier is not None else None
        platform = account["platform"] if account else None
        for field in ("privacy", "for_kids", "synthetic"):
            self.form.show_field(field, platform == "YouTube")
        self.form.show_field("video_url", platform == "Instagram")
        hints = {
            "YouTube": "YouTube Shorts · выберите видимость и отметьте особенности контента.",
            "Instagram": "Instagram Reels · укажите прямую публичную HTTPS-ссылку на этот файл.",
        }
        self.platform_hint.setText(hints.get(platform, "Выберите аккаунт и видео. Время указывается по часам Windows."))

    def refresh(self):
        choices(self.form.widget("account_id"), ((f"{r['name']} · {r['platform']}", r["id"]) for r in self.db.accounts()),
                first="Выберите аккаунт")
        choices(self.form.widget("video_id"), ((r["title"], r["id"]) for r in self.db.videos()), first="Выберите видео")
        rows = self.db.posts()
        day = self.calendar.selectedDate().toString("yyyy-MM-dd")
        dates = {datetime.fromisoformat(row["due"]).astimezone().date().isoformat() for row in rows}
        for old in self.marked:
            self.calendar.setDateTextFormat(old, QTextCharFormat())
        self.marked.clear()
        fmt = QTextCharFormat()
        fmt.setFontWeight(700)
        for entry in dates:
            calendar_day = QDate.fromString(entry, "yyyy-MM-dd")
            self.calendar.setDateTextFormat(calendar_day, fmt)
            self.marked.append(calendar_day)
        visible = [row for row in rows if not self.day_only.isChecked() or
                   datetime.fromisoformat(row["due"]).astimezone().date().isoformat() == day]
        self.queue_title.setText(f"Очередь · {len(visible)} из {len(rows)}" if self.day_only.isChecked()
                                 else f"Очередь · {len(rows)}")
        self.table.empty_text("На выбранный день публикаций нет." if rows else
                              "Очередь пока пуста. Создайте первый черновик.")
        self.table.fill(((row["id"], (local_time(row["due"]), f"{row['title']}  ·  {row['account']}",
                                      LABELS[row["state"]])) for row in visible), self.editing)
        self.platform_fields()
        if self.editing is not None and self.table.selected() != self.editing:
            self.clear_editor()
        elif self.editing is not None:
            self.update_buttons(self.db.post(self.editing))

    def new(self, video=None):
        self.refresh()
        self.clear_editor(video)

    def clear_editor(self, video=None):
        self.editing = None
        self.saved_form = None
        self.video_defaults = {}
        blocked = self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(blocked)
        self.form.set(dict(account_id=None, video_id=video, title="", caption="", topic="",
                           due=datetime.now().strftime("%Y-%m-%d %H:%M"), privacy="private",
                           for_kids=False, synthetic=False, video_url=""))
        self.heading.setText("Новый черновик")
        self.result.clear()
        self.log.fill([])
        self.link.set({"remote": ""})
        self.platform_fields()
        self.update_buttons()
        self.fill_title()

    def update_buttons(self, row=None):
        state = row["state"] if row else None
        editable = state in (None, "planned")
        self.form.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.save_button.setVisible(editable)
        test_mode = self.db.setting("test_mode", "1") != "0"
        self.send_button.setText("Проверить черновик" if test_mode else "Отправить…")
        self.send_button.setEnabled(state == "planned")
        self.send_button.setVisible(state == "planned")
        self.resume_button.setEnabled(state == "sending" and not test_mode)
        self.resume_button.setVisible(state == "sending" and not test_mode)
        self.check_button.setEnabled(state not in (None, "planned"))
        self.check_button.setVisible(state not in (None, "planned"))
        has_log = self.log.rowCount() > 0
        self.log_title.setVisible(has_log)
        self.log.setVisible(has_log)
        can_link = bool(row and not row["remote_id"] and (state == "restored" or
                        (state == "confirmed" and row["platform"] == "Instagram")))
        self.link.setVisible(can_link)
        self.link_button.setVisible(can_link)
        self.reset.setVisible(bool(row and row["platform"] == "Instagram" and state == "failed"
                                   and not row["remote_id"]))
        self.release.setVisible(bool(state == "restored" and not
                                     (row["started"] or row["container"] or row["remote_id"])))

    def fill_title(self):
        video = self.form.widget("video_id").currentData()
        row = self.db.get("SELECT title,description FROM videos WHERE id=?", video) if video else None
        defaults = {"title": row["title"], "caption": row["description"]} if row else {"title": "", "caption": ""}
        current = {"title": self.form.widget("title").text(),
                   "caption": self.form.widget("caption").toPlainText()}
        self.form.set({key: value for key, value in defaults.items()
                       if not current[key] or current[key] == self.video_defaults.get(key)})
        self.video_defaults = defaults

    def load(self):
        selected = self.table.selected()
        if selected is None:
            if self.editing is not None:
                self.clear_editor()
            return
        if selected != self.editing:
            self.link.set({"remote": ""})
        self.editing = selected
        self.load_details()

    def load_details(self):
        row = self.db.post(self.editing)
        data = {key: row[key] for key in self.form.fields}
        data["due"] = datetime.fromisoformat(row["due"]).astimezone().strftime("%Y-%m-%d %H:%M")
        self.form.set(data)
        self.saved_form = self.form.values()
        video = self.db.get("SELECT title,description FROM videos WHERE id=?", row["video_id"])
        self.video_defaults = {"title": video["title"], "caption": video["description"]}
        self.heading.setText(f"№{row['id']} · {LABELS[row['state']]}")
        identity = f" · ID {row['remote_id']}" if row["remote_id"] else ""
        self.result.setText(row["result"] + identity)
        self.log.fill((i, (local_time(item["at"]), item["message"]))
                      for i, item in enumerate(self.db.post_log(row["id"])))
        self.platform_fields()
        self.update_buttons(row)

    def save(self):
        data = self.form.values()
        account = self.db.account(required(data["account_id"], "аккаунт"))
        required(data["video_id"], "видео")
        if account["platform"] == "Instagram":
            data.update(privacy="private", for_kids=False, synthetic=False)
        else:
            data["video_url"] = ""
        try:
            local = datetime.strptime(data["due"], "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ.") from None
        # Windows uses local time here. Reject an invalid clock time at a DST transition.
        early = local.replace(fold=0).astimezone(UTC)
        late = local.replace(fold=1).astimezone(UTC)
        if early != late:
            raise ValueError("Это время неоднозначно из-за смены часового пояса. Выберите другое время.")
        data["due"] = early.isoformat()
        self.editing = self.db.save_post(data, self.editing)
        if self.day_only.isChecked():
            blocked = self.calendar.blockSignals(True)
            self.calendar.setSelectedDate(QDate(local.year, local.month, local.day))
            self.calendar.blockSignals(blocked)
        self.refresh()
        self.load_details()
        self.window.notice("Запись сохранена в очереди.")

    def selected(self):
        return required(self.table.selected(), "видимую запись в очереди")

    def send(self, resume):
        identifier = self.selected()
        post = self.db.post(identifier)
        if post["state"] != ("sending" if resume else "planned"):
            raise ValueError("Эта запись недоступна для отправки. Проверьте её состояние.")
        if not resume and self.form.values() != self.saved_form:
            raise ValueError("Сохраните изменения черновика перед проверкой или отправкой.")
        if self.db.setting("test_mode", "1") == "0":
            account = self.db.account(post["account_id"])
            account_name = account["remote_name"] or account["name"]
            target = post["privacy"] if post["platform"] == "YouTube" else "Reels из указанного HTTPS-файла"
            if not self.window.confirm("Подтверждение отправки",
                                       f"Отправить «{post['title']}» в {post['platform']} · {account_name}?\n{target}"):
                return
        self.window.job("Проверка и обработка публикации", lambda: self.publisher.send(identifier, resume),
                        self.completed, error=self.update_current)

    def check(self):
        identifier = self.selected()
        self.window.job("Сверка с официальным API", lambda: self.publisher.check(identifier), self.completed,
                        error=self.update_current)

    def associate(self):
        identifier = self.selected()
        remote = self.link.values()["remote"]
        if not remote:
            raise ValueError("Укажите ID публикации.")
        if not self.window.confirm("Связать публикацию", f"Связать запись №{identifier} с публикацией {remote}?"):
            return
        self.window.job("Проверка ID публикации", lambda: self.publisher.associate(identifier, remote), self.completed)

    def retry_container(self):
        identifier = self.selected()
        if self.window.confirm("Вернуть запись в очередь", "Отклонённый контейнер будет заменён при следующей отправке."):
            self.db.reset_unpublished_reel(identifier)
            self.completed("Запись возвращена. Проверьте видео и адрес перед отправкой.")

    def release_draft(self):
        identifier = self.selected()
        if self.window.confirm("Сверка восстановленного черновика",
                               "Подтвердите, что вы проверили аккаунт на платформе и этот ролик там отсутствует."):
            self.db.release_restored_draft(identifier)
            self.completed("Проверенный черновик возвращён в очередь.")

    def update_current(self):
        self.refresh()
        if self.editing:
            self.load_details()

    def completed(self, message):
        self.update_current()
        self.window.notice(message or "Запись обновлена.")


class Analytics(Page):
    def __init__(self, window):
        super().__init__(window, "Отчёт за завершённые дни. Метрики публикаций накопительные, сравнение тем показывает покрытие данных.")
        self.publisher = Publisher(self.db, self.vault)
        self.account = QComboBox()
        self.account.setMinimumWidth(215)
        self.days = QComboBox()
        choices(self.days, [("7 дней", 7), ("30 дней", 30)])
        for control in (self.account, self.days):
            control.currentIndexChanged.connect(self.guard(self.show_report))
        self.layout.addLayout(toolbar(self.account, self.days, button("Собрать через API", self.guard(self.collect), True)))
        self.period = label("", "muted")
        self.layout.addWidget(self.period)
        tabs = QTabWidget()
        self.summary = Table(("Показатель", "Значение"), empty="Добавьте аккаунт для аналитики.")
        self.posts = Table(("Публикация", "Тема", "Просмотры", "Охват", "Взаимодействия", "Лайки", "Комментарии",
                            "Репосты", "Сохранения"), empty="Здесь появятся данные связанных публикаций.")
        self.topics = Table(("Тема", "Публикации", "Данные о просмотрах", "Известные просмотры"),
                            empty="Пока нет публикаций для сравнения.")
        tabs.addTab(self.summary, "За период")
        tabs.addTab(self.posts, "Публикации · накопительно")
        tabs.addTab(self.topics, "Темы · накопительно")
        self.layout.addWidget(tabs, 1)

    def refresh(self):
        choices(self.account, ((f"{row['name']} · {row['platform']}", row["id"]) for row in self.db.accounts()))
        self.show_report()

    def show_report(self):
        account = self.account.currentData()
        days = self.days.currentData()
        values, posts, topics = report(self.db, account, days) if account else ({}, [], {})
        if account:
            today = datetime.now(reporting_zone(self.db.account(account))).date()
            sample = values["period_day"]
            collected = date.fromisoformat(sample) if sample else today
            start, end = report_period(days, collected - timedelta(days=1))
            detail = f"собрано {collected:%d.%m.%Y}" if sample else "данные ещё не собраны"
            if collected < today:
                detail += " · обновите через API"
            self.period.setText(f"Период {start} — {end} · {detail}")
        else:
            self.period.setText("Добавьте аккаунт для сбора статистики.")
        self.summary.fill((key, (name, values.get(key))) for key, name in METRICS.items() if account)
        self.posts.fill((i, (p["title"], p["topic"], *(p.get(key) for key in
                         ("views", "reach", "total_interactions", "likes", "comments", "shares", "saved"))))
                        for i, p in enumerate(posts))
        self.topics.fill((topic, (topic, group["posts"], f"{group['coverage']} из {group['posts']}",
                                group["views"] if group["coverage"] else None))
                         for topic, group in sorted(topics.items()))

    def collect(self):
        account = required(self.account.currentData(), "аккаунт")
        days = self.days.currentData()
        self.window.job("Запрос статистики", lambda: self.publisher.collect(account, days), self.collected)

    def collected(self, message):
        self.show_report()
        self.window.notice(message)
