"""Local account preparation embedded in the chat."""

from uuid import uuid4
import json

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget

from ..db import https_url
from ..registration import Registration, SIGNUP_URLS
from .common import Fields, Page, Table, button, label, required, scroll, surface, toolbar


class RegistrationPanel(Page):
    def __init__(self, window):
        super().__init__(window, "Здесь сохраняются карточки, данные для входа и материалы. "
                         "Внешнюю регистрацию подтверждает сам сервис.")
        self.manager = Registration(self.db, self.vault)
        self.batch_id = self.db.setting("registration_batch_active") or uuid4().hex
        self.active = None
        columns = QHBoxLayout()
        self.layout.addLayout(columns, 1)
        left = QWidget()
        left.setMinimumWidth(215)
        left.setMaximumWidth(270)
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 8, 0)
        left_box.addWidget(label("Новый пакет", "section"))
        self.form = Fields()
        self.form.add("platform", "Платформа", "choice", [(p, p) for p in SIGNUP_URLS])
        self.form.add("title", "Название")
        self.form.add("topic", "Тематика")
        self.form.add("count", "Карточек", "int", (1, 50))
        self.form.set({"title": "NOVA", "topic": "Виртуальная ИИ-модель", "count": 1})
        try:
            saved_form = json.loads(self.db.setting("registration_form", "{}"))
            if isinstance(saved_form, dict):
                self.form.set(saved_form)
        except (TypeError, ValueError):
            pass
        left_box.addWidget(self.form)
        self.create_button = button("Подготовить пакет", self.guard(self.create_batch), True)
        left_box.addWidget(self.create_button)
        left_box.addWidget(button("Начать новый пакет", self.new_batch))
        left_box.addWidget(label("Сохраняется локальный пакет до 50 карточек. "
                                "Каждая внешняя регистрация проходит свои проверки и подтверждение условий.", "muted"))
        left_box.addStretch(1)
        columns.addWidget(left)
        right = QVBoxLayout()
        columns.addLayout(right, 1)
        self.table = Table(("Платформа", "Карточка", "Внешний профиль"), stretch=1,
                           empty="Подготовьте карточку или пакет слева.")
        self.table.itemSelectionChanged.connect(self.guard(self.select))
        self.table.verticalHeader().setDefaultSectionSize(36)
        right.addWidget(self.table, 1)
        details, box = surface()
        self.heading = label("Выберите карточку", "section")
        self.state = label("", "muted")
        box.addWidget(self.heading)
        box.addWidget(self.state)
        self.login = QLineEdit()
        self.login.setReadOnly(True)
        self.login.setPlaceholderText("Предложенный логин появится после подготовки")
        self.login.setAccessibleName("Предложенный логин аккаунта")
        box.addWidget(self.login)
        secret_line = QHBoxLayout()
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setReadOnly(True)
        self.secret.setAccessibleName("Пароль из защищённого хранилища")
        self.secret.setPlaceholderText("Пароль хранится в Windows")
        secret_line.addWidget(self.secret, 1)
        self.reveal = button("Показать", self.reveal_password)
        self.reveal.setCheckable(True)
        secret_line.addWidget(self.reveal)
        box.addLayout(secret_line)
        self.prepare_button = button("Подготовить логин и пароль", self.guard(self.prepare))
        self.copy_login = button("Копировать логин", self.guard(lambda: self.copy_credential("login")))
        self.copy_password = button("Копировать пароль", self.guard(lambda: self.copy_credential("password")))
        box.addLayout(toolbar(self.prepare_button))
        box.addLayout(toolbar(self.copy_login, self.copy_password))
        self.signup = button("Открыть регистрацию", self.guard(self.open_signup), True)
        self.profile = button("Открыть профиль", self.guard(self.open_profile))
        box.addLayout(toolbar(self.signup, self.profile))
        self.avatar = label("")
        self.avatar.setFixedSize(76, 76)
        self.description = label("", "muted")
        visual = QHBoxLayout()
        visual.addWidget(self.avatar)
        visual.addWidget(self.description, 1)
        box.addLayout(visual)
        self.files = button("Материалы", self.guard(lambda: self.open_material("folder")))
        self.media = button("Видео и аватар", self.guard(lambda: self.open_material("media")))
        self.copy_bio = button("Копировать описание", self.guard(self.copy_description))
        box.addLayout(toolbar(self.files, self.media, self.copy_bio))
        right.addWidget(scroll(details), 2)
        for key in self.form.fields:
            widget = self.form.widget(key)
            signal = widget.currentIndexChanged if key == "platform" else widget.valueChanged if key == "count" else widget.textChanged
            signal.connect(self.new_batch)
        self.mask_timer = QTimer(self)
        self.mask_timer.setSingleShot(True)
        self.mask_timer.setInterval(30000)
        self.mask_timer.timeout.connect(self.mask_password)

    def new_batch(self, *_args):
        self.batch_id = uuid4().hex
        self.save_form()

    def save_form(self):
        self.db.settings({"registration_form": json.dumps(self.form.values(), ensure_ascii=False),
                          "registration_batch_active": self.batch_id})

    def refresh(self, account_id=None):
        if account_id is not None:
            self.active = account_id
        accounts = self.db.accounts()
        if self.active is None and accounts:
            saved = self.db.setting("registration_active", "")
            self.active = next((a["id"] for a in accounts if str(a["id"]) == saved), accounts[0]["id"])
        self.table.fill(((a["id"], (a["platform"], a["name"],
                          "API подключён" if a["remote_id"] else "Ссылка указана" if a["profile"] else "Не подтверждён"))
                         for a in accounts), self.active)
        self.select()

    def create_batch(self):
        data = self.form.values()
        self.save_form()
        batch_id = self.batch_id
        self.window.job("Подготовка локальных карточек", lambda: self.manager.batch(
            data["platform"], data["title"], data["topic"], data["count"], batch_id), self.batch_finished)

    def batch_finished(self, identifiers):
        self.refresh(identifiers[0])
        self.window.notice(f"Сохранён локальный пакет: {len(identifiers)} карточек. Внешние аккаунты ещё не зарегистрированы.")

    def selected(self):
        return self.db.account(required(self.active, "карточку"))

    def select(self):
        self.active = self.table.selected()
        if self.active is not None:
            self.db.settings({"registration_active": str(self.active)})
        self.mask_password()
        account = self.db.account(self.active) if self.active is not None else None
        record = self.manager.credentials(self.active) if account else None
        self.heading.setText(account["name"] if account else "Выберите карточку")
        self.state.setText("" if account is None else self.db.setting(
            f"registration_step/{self.active}", "Данные подготовлены локально. Регистрация и установка оформления не подтверждены.")
            if not account["profile"] else "Ссылка на профиль указана. Статус подключения виден в разделе «Аккаунты».")
        self.login.setText(record["login"] if record else "")
        self.secret.setText(record["password"] if record else "")
        for control in (self.reveal, self.copy_login, self.copy_password):
            control.setEnabled(bool(record))
        self.prepare_button.setVisible(bool(account and not record))
        self.signup.setEnabled(bool(account))
        self.profile.setEnabled(bool(account and account["profile"]))
        materials = self.manager.materials(self.active) if account else {}
        try:
            avatar = self.manager.material_path(self.active, "avatar") if materials else None
        except ValueError:
            avatar = None
        self.avatar.clear()
        self.avatar.setVisible(avatar is not None)
        if avatar and avatar.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            self.avatar.setPixmap(QPixmap(str(avatar)).scaled(76, 76, Qt.AspectRatioMode.KeepAspectRatio,
                                                           Qt.TransformationMode.SmoothTransformation))
        self.description.setText(materials.get("description", "Материалы оформления ещё не прикреплены."))
        self.files.setEnabled(bool(materials.get("folder")))
        self.media.setEnabled(bool(materials.get("media")))
        self.copy_bio.setEnabled(bool(materials.get("description")))

    def prepare(self):
        self.manager.prepare_credentials(self.selected()["id"])
        self.refresh(self.active)

    def mask_password(self):
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.reveal.setChecked(False)
        self.reveal.setText("Показать")

    def reveal_password(self):
        self.secret.setEchoMode(QLineEdit.EchoMode.Normal if self.reveal.isChecked() else QLineEdit.EchoMode.Password)
        self.reveal.setText("Скрыть" if self.reveal.isChecked() else "Показать")
        if self.reveal.isChecked():
            self.mask_timer.start()

    def copy_credential(self, field):
        value = (self.manager.credentials(self.selected()["id"]) or {}).get(field)
        if not value:
            raise ValueError("Сначала подготовьте данные для входа.")
        QGuiApplication.clipboard().setText(value)
        if field == "password":
            QTimer.singleShot(30000, lambda: QGuiApplication.clipboard().clear()
                              if QGuiApplication.clipboard().text() == value else None)
        self.window.notice("Скопирован пароль. Буфер очистится через 30 секунд." if field == "password" else "Логин скопирован.")

    def copy_description(self):
        text = self.manager.materials(self.selected()["id"]).get("description", "")
        if text:
            QGuiApplication.clipboard().setText(text)

    def open_signup(self):
        QDesktopServices.openUrl(QUrl(SIGNUP_URLS[self.selected()["platform"]]))

    def open_profile(self):
        url = self.selected()["profile"]
        if not url:
            raise ValueError("Ссылка на реальный профиль пока не указана.")
        https_url(url)
        QDesktopServices.openUrl(QUrl(url))

    def open_material(self, field):
        path = self.manager.material_path(self.selected()["id"], field)
        if path is None:
            raise ValueError("Материалы ещё не прикреплены.")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
