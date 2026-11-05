from PySide6.QtCore import QByteArray, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QProgressBar,
    QScrollArea, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from ..chat import ChatService, ChatStore, DEFAULT_MODEL, KEY_NAME, chat_config, remember_models, saved_models, validate_model
from .common import Page, Worker, button, choices, label, surface


class Composer(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MessageText(QTextBrowser):
    def __init__(self, text, markdown=True):
        super().__init__()
        self.setObjectName("chatText")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(32)
        self.document().setDocumentMargin(0)
        if markdown:
            self.document().setMarkdown(
                text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
        else:
            self.document().setPlainText(text)
        self.anchorClicked.connect(self.open_link)
        self.document().contentsChanged.connect(self.fit)

    def loadResource(self, _kind, _url):
        return QByteArray()

    def open_link(self, url):
        if url.scheme().lower() in {"https", "http"}:
            QDesktopServices.openUrl(url)

    def fit(self):
        self.document().setTextWidth(max(80, self.viewport().width()))
        height = max(32, int(self.document().size().height()) + 7)
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.fit)


class Chat(Page):
    def __init__(self, window):
        super().__init__(window, "")
        self.layout.itemAt(0).widget().hide()
        self.store = ChatStore(self.db)
        self.service = ChatService(self.db, self.vault)
        self.chat_id = None
        self.draft_model = self.db.setting("chat_model", DEFAULT_MODEL)
        self.busy = False
        self.request_kind = None
        self.worker = None
        self.drafts = {}
        self.last_text = ""
        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        self.layout.addWidget(self.split, 1)
        self.rail, rail_box = surface("chatRail")
        self.rail.setMinimumWidth(165)
        self.rail.setMaximumWidth(245)
        self.new_button = button("＋  Новая беседа", self.guard(self.new_chat), True)
        rail_box.addWidget(self.new_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск бесед")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_history)
        rail_box.addWidget(self.search)
        rail_box.addWidget(label("ИСТОРИЯ", "eyebrow"))
        self.sessions = QListWidget()
        self.sessions.setObjectName("chatHistory")
        self.sessions.setWordWrap(True)
        self.sessions.setAccessibleName("История бесед")
        self.sessions.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.sessions.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sessions.customContextMenuRequested.connect(self.history_menu)
        self.sessions.currentItemChanged.connect(self.guard(self.select_chat))
        self.sessions.itemChanged.connect(self.rename_chat)
        rail_box.addWidget(self.sessions, 1)
        self.rail_note = label("Чаты сохраняются при смене модели.", "muted")
        rail_box.addWidget(self.rail_note)
        self.split.addWidget(self.rail)
        work = QWidget()
        center = QVBoxLayout(work)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(10)
        top = QHBoxLayout()
        self.history_toggle = button("Беседы", self.toggle_history)
        self.history_toggle.setCheckable(True)
        self.history_toggle.setChecked(True)
        top.addWidget(self.history_toggle)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setMinimumWidth(170)
        self.model.setMaximumWidth(235)
        self.model.setAccessibleName("Модель GPT для этой беседы")
        self.model.setToolTip("Выберите модель или введите её ID. Старые ответы сохраняются.")
        self.model.activated.connect(self.guard(self.select_model))
        self.model.lineEdit().editingFinished.connect(self.guard(self.select_model))
        top.addWidget(self.model)
        self.catalog_button = button("↻", self.guard(self.load_models))
        self.catalog_button.setFixedWidth(34)
        self.catalog_button.setToolTip("Обновить доступные GPT-модели через OpenAI")
        top.addWidget(self.catalog_button)
        top.addStretch(1)
        top.addWidget(button("Настройки", self.window.open_chat_settings))
        center.addLayout(top)
        self.thread_title = label("Новая беседа", "section")
        center.addWidget(self.thread_title)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _minimum, maximum: self.scroll.verticalScrollBar().setValue(maximum))
        self.canvas = QWidget()
        self.canvas.setObjectName("chatCanvas")
        self.messages = QVBoxLayout(self.canvas)
        self.messages.setContentsMargins(12, 12, 12, 12)
        self.messages.setSpacing(16)
        self.scroll.setWidget(self.canvas)
        center.addWidget(self.scroll, 1)
        self.connection = label("", "muted")
        self.configure_button = button("Подключить OpenAI", self.window.open_chat_settings)
        notice = QHBoxLayout()
        notice.addWidget(self.connection, 1)
        notice.addWidget(self.configure_button)
        center.addLayout(notice)
        composer, box = surface("chatComposer")
        self.editor = Composer()
        self.editor.setObjectName("chatInput")
        self.editor.setPlaceholderText("Сообщение…")
        self.editor.setMinimumHeight(54)
        self.editor.setMaximumHeight(102)
        self.editor.setAccessibleName("Сообщение ИИ-чату")
        self.editor.submitted.connect(self.guard(self.send))
        box.addWidget(self.editor)
        footer = QHBoxLayout()
        self.account = QComboBox()
        self.account.setMinimumContentsLength(11)
        self.account.setMaximumWidth(265)
        self.account.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.account.setAccessibleName("Контекст аккаунта")
        self.account.currentIndexChanged.connect(self.update_context)
        footer.addWidget(self.account)
        footer.addStretch(1)
        self.send_button = button("↑", self.guard(self.send), True)
        self.send_button.setFixedSize(39, 37)
        self.send_button.setAccessibleName("Отправить сообщение")
        self.send_button.setToolTip("Отправить · Enter")
        footer.addWidget(self.send_button)
        box.addLayout(footer)
        center.addWidget(composer)
        self.context = label("", "chatFootnote")
        center.addWidget(self.context)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        center.addWidget(self.progress)
        self.split.addWidget(work)
        self.split.setSizes([195, 765])

    def toggle_history(self):
        self.rail.setVisible(self.history_toggle.isChecked())

    def filter_history(self, *_args):
        needle = self.search.text().casefold().strip()
        for index in range(self.sessions.count()):
            item = self.sessions.item(index)
            item.setHidden(needle not in item.toolTip().casefold())

    def history_menu(self, position):
        item = self.sessions.itemAt(position)
        if item is None:
            return
        menu = QMenu(self.sessions)
        rename = menu.addAction("Переименовать")
        if menu.exec(self.sessions.mapToGlobal(position)) == rename:
            self.sessions.editItem(item)

    def rename_chat(self, item):
        identifier, title = item.data(Qt.ItemDataRole.UserRole), item.text()
        self.window.guard(lambda: self.store.rename(identifier, title))
        self.refresh()

    def refresh(self):
        chats = self.store.chats()
        selected = self.chat_id
        blocked = self.sessions.blockSignals(True)
        self.sessions.clear()
        for chat in chats:
            item = QListWidgetItem(chat["title"])
            item.setData(Qt.ItemDataRole.UserRole, chat["id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setToolTip(chat["title"] + ("\n" + chat["account"] if chat["account"] else ""))
            self.sessions.addItem(item)
            if chat["id"] == selected:
                self.sessions.setCurrentItem(item)
        self.sessions.blockSignals(blocked)
        self.filter_history()
        row = self.db.get("SELECT * FROM chats WHERE id=?", selected) if selected is not None else None
        self.thread_title.setText(row["title"] if row else "Новая беседа")
        choices(self.account, ((r["name"], r["id"]) for r in self.db.accounts()),
                selected=row["account_id"] if row else self.account.currentData(), first="Без аккаунта")
        if row and row["account_id"] is None:
            self.account.setCurrentIndex(0)
        self.account.setEnabled(self.chat_id is None and not self.busy)
        selected_model = (row["model"] or self.db.setting("chat_model", DEFAULT_MODEL)) if row else self.draft_model
        models = list(dict.fromkeys([*saved_models(self.db), selected_model]))
        choices(self.model, ((model, model) for model in models), selected=selected_model)
        self.model.setEnabled(not self.busy)
        configured = bool(self.vault.get(KEY_NAME))
        self.send_button.setEnabled(configured and not self.busy)
        self.catalog_button.setEnabled(configured and not self.busy)
        self.new_button.setEnabled(not self.busy)
        self.sessions.setEnabled(not self.busy)
        self.editor.setEnabled(not self.busy)
        self.connection.setText("" if configured else "Добавьте API-ключ, чтобы получать ответы.")
        self.connection.setVisible(not configured)
        self.configure_button.setVisible(not configured)
        self.update_context()
        self.show_messages()

    def select_model(self):
        if self.busy:
            return
        model = validate_model(self.model.currentText().strip())
        remember_models(self.db, [model])
        if self.chat_id is not None:
            self.store.set_model(self.chat_id, model)
        self.draft_model = model
        self.db.settings({"chat_model": model})

    def update_context(self, *_args):
        with_account = self.account.currentData() is not None
        self.account.setToolTip("Тематика, аудитория и идеи этого аккаунта будут переданы с сообщением."
                                if with_account else "Выберите аккаунт для новой беседы, если нужен его контекст.")
        self.context.setText("Enter — отправить · Shift+Enter — новая строка" +
                             (" · Контекст выбранного аккаунта включён" if with_account else ""))

    def use_prompt(self, text):
        if not self.busy:
            self.editor.setPlainText(text)
            self.editor.setFocus()

    def new_chat(self):
        if self.busy:
            raise ValueError("Дождитесь ответа.")
        self.drafts[self.chat_id] = self.editor.toPlainText()
        self.chat_id = None
        self.editor.setPlainText(self.drafts.get(None, ""))
        self.refresh()
        self.editor.setFocus()

    def select_chat(self):
        item = self.sessions.currentItem()
        if item is None:
            return
        selected = item.data(Qt.ItemDataRole.UserRole)
        if selected == self.chat_id:
            return
        self.drafts[self.chat_id] = self.editor.toPlainText()
        self.chat_id = selected
        self.editor.setPlainText(self.drafts.get(selected, ""))
        self.refresh()

    def show_messages(self):
        while self.messages.count():
            item = self.messages.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        rows = self.store.messages(self.chat_id) if self.chat_id is not None else []
        if self.busy and self.request_kind == "message" and not any(row["state"] == "pending" for row in rows):
            rows.append({"role": "user", "body": self.last_text, "state": "pending", "error": "", "model": ""})
        if not rows:
            self.messages.addStretch(1)
            welcome = QWidget()
            welcome.setObjectName("chatWelcome")
            welcome_box = QVBoxLayout(welcome)
            welcome_box.setContentsMargins(10, 10, 10, 10)
            welcome_box.setSpacing(12)
            title = label("С чего начнём?", "chatHero")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            welcome_box.addWidget(title)
            hint = label("Придумайте образ, соберите сценарий или спланируйте контент.", "muted")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            welcome_box.addWidget(hint)
            cards = QGridLayout()
            cards.setSpacing(9)
            prompts = (
                ("Образ персонажа", "Стиль, характер, подача",
                 "Помоги придумать оригинальный образ виртуальной ИИ-модели: характер, стиль и подачу."),
                ("Сценарий Shorts", "Хук и структура на 30 секунд",
                 "Помоги написать сценарий оригинального Shorts на 30 секунд. Уточни цель и аудиторию."),
                ("Контент-план", "Идеи на ближайшие 14 дней",
                 "Составь контент-план на 14 дней для моей темы с оригинальными идеями Shorts и Reels."),
                ("Описание профиля", "Коротко и в нужном тоне",
                 "Помоги составить описание профиля виртуальной ИИ-модели с прозрачным обозначением AI-персонажа."),
            )
            for index, (name, detail, prompt) in enumerate(prompts):
                item = button(name + "\n" + detail, lambda _checked, text=prompt: self.use_prompt(text))
                item.setObjectName("chatPrompt")
                item.setMinimumHeight(57)
                cards.addWidget(item, index // 2, index % 2)
            welcome_box.addLayout(cards)
            self.messages.addWidget(welcome)
        for message in rows:
            is_user = message["role"] == "user"
            bubble, box = surface("chatUser" if is_user else "chatAssistant")
            if not is_user:
                box.addWidget(label(message.get("model") or "ИИ-помощник", "eyebrow"))
            box.addWidget(MessageText(message["body"], markdown=not is_user))
            footer = QHBoxLayout()
            if message["state"] == "failed":
                footer.addWidget(label(message["error"], "muted"), 1)
            elif message["state"] == "pending":
                footer.addWidget(label("Готовлю ответ…", "muted"), 1)
            else:
                footer.addStretch(1)
            copy = button("Копировать", lambda _checked, text=message["body"]:
                          QGuiApplication.clipboard().setText(text))
            copy.setObjectName("chatCopy")
            footer.addWidget(copy)
            box.addLayout(footer)
            if is_user:
                bubble.setMaximumWidth(660)
                self.messages.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
            else:
                self.messages.addWidget(bubble)
        self.messages.addStretch(1)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def set_busy(self, kind):
        self.busy = True
        self.request_kind = kind
        self.progress.show()
        self.refresh()
        self.connection.setVisible(True)
        self.connection.setText("Готовлю ответ…" if kind == "message" else "Обновляю список моделей…")
        self.configure_button.hide()

    def send(self):
        text = self.editor.toPlainText().strip()
        if self.busy or not text:
            return
        chat_config(self.db, self.vault)
        self.select_model()
        model = validate_model(self.model.currentText().strip())
        if self.chat_id is None:
            self.chat_id = self.store.create(self.account.currentData(), model)
            self.drafts.pop(None, None)
        self.last_text = text
        chat_id = self.chat_id
        self.set_busy("message")
        self.worker = Worker("Ответ ИИ-чата", lambda: self.service.send(chat_id, text))
        self.worker.signals.completed.connect(self.finished)
        self.worker.signals.error.connect(self.failed)
        QThreadPool.globalInstance().start(self.worker)

    def load_models(self):
        if self.busy:
            return
        self.set_busy("catalog")
        self.worker = Worker("Список моделей OpenAI", self.service.catalog)
        self.worker.signals.completed.connect(self.catalog_finished)
        self.worker.signals.error.connect(self.failed)
        QThreadPool.globalInstance().start(self.worker)

    def end_request(self):
        self.busy = False
        self.worker = None
        self.progress.hide()

    @Slot(object)
    def finished(self, _answer):
        self.end_request()
        self.editor.clear()
        self.drafts.pop(self.chat_id, None)
        self.refresh()

    @Slot(object)
    def catalog_finished(self, _models):
        self.end_request()
        self.refresh()
        self.window.notice("Список обновлён. Ранее выбранные модели сохранены.")

    @Slot(str)
    def failed(self, message):
        kind = self.request_kind
        self.end_request()
        self.refresh()
        self.connection.setVisible(True)
        self.connection.setText(message)
        if kind == "message":
            self.editor.setPlainText(self.last_text)
