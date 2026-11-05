from PySide6.QtCore import QByteArray, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QTextDocument
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QPlainTextEdit, QProgressBar,
    QScrollArea, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from ..chat import ChatService, ChatStore, DEFAULT_MODEL, KEY_NAME, chat_config
from .common import Page, Worker, button, choices, label, surface, toolbar


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
    def __init__(self, text):
        super().__init__()
        self.setObjectName("chatText")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMinimumHeight(32)
        self.document().setDocumentMargin(0)
        self.document().setMarkdown(
            text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
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
        super().__init__(window, "Идеи, сценарии и контент-планы с сохранением переписки.")
        self.store = ChatStore(self.db)
        self.service = ChatService(self.db, self.vault)
        self.chat_id = None
        self.busy = False
        self.worker = None
        self.drafts = {}
        self.last_text = ""
        split = QSplitter()
        split.setChildrenCollapsible(False)
        self.layout.addWidget(split, 1)
        rail, rail_box = surface()
        rail.setMinimumWidth(165)
        rail.setMaximumWidth(255)
        self.new_button = button("＋ Новый чат", self.guard(self.new_chat), True)
        rail_box.addWidget(self.new_button)
        rail_box.addWidget(label("БЕСЕДЫ", "eyebrow"))
        self.sessions = QListWidget()
        self.sessions.setObjectName("chatHistory")
        self.sessions.setWordWrap(True)
        self.sessions.setAccessibleName("История бесед")
        self.sessions.currentItemChanged.connect(self.guard(self.select_chat))
        rail_box.addWidget(self.sessions, 1)
        rail_box.addWidget(label("История хранится рядом с базой.", "muted"))
        split.addWidget(rail)
        work = QWidget()
        center = QVBoxLayout(work)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(9)
        self.account = QComboBox()
        self.account.setMinimumContentsLength(15)
        self.account.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.account.setAccessibleName("Контекст аккаунта")
        self.account.currentIndexChanged.connect(self.update_context)
        self.model = label("", "muted")
        center.addLayout(toolbar(self.account, self.model,
                                 button("Настройки чата", self.window.open_chat_settings)))
        self.context = label("", "muted")
        center.addWidget(self.context)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.canvas = QWidget()
        self.canvas.setObjectName("chatCanvas")
        self.messages = QVBoxLayout(self.canvas)
        self.messages.setContentsMargins(4, 10, 4, 10)
        self.messages.setSpacing(13)
        self.scroll.setWidget(self.canvas)
        center.addWidget(self.scroll, 1)
        suggestions = QHBoxLayout()
        suggestions.setSpacing(6)
        prompts = (
            ("Идеи Reels", "Предложи 10 оригинальных идей Reels для моей темы. Для каждой укажи хук и структуру."),
            ("Сценарий Shorts", "Помоги написать сценарий оригинального Shorts на 30 секунд. Сначала уточни тему и цель."),
            ("Контент-план", "Составь контент-план на 14 дней для моей темы с идеями видео и задачами подготовки."),
        )
        for title, prompt in prompts:
            suggestions.addWidget(button(title, lambda _checked, text=prompt: self.use_prompt(text)))
        center.addLayout(suggestions)
        self.connection = label("", "muted")
        center.addWidget(self.connection)
        self.editor = Composer()
        self.editor.setPlaceholderText("Напишите сообщение…")
        self.editor.setMinimumHeight(80)
        self.editor.setMaximumHeight(122)
        self.editor.setAccessibleName("Сообщение ИИ-чату")
        self.editor.submitted.connect(self.guard(self.send))
        center.addWidget(self.editor)
        self.send_button = button("Отправить", self.guard(self.send), True)
        footer = QHBoxLayout()
        footer.addWidget(label("Enter — отправить · Shift+Enter — новая строка", "muted"), 1)
        footer.addWidget(self.send_button)
        center.addLayout(footer)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        center.addWidget(self.progress)
        split.addWidget(work)
        split.setSizes([200, 760])

    def refresh(self):
        chats = self.store.chats()
        selected = self.chat_id
        blocked = self.sessions.blockSignals(True)
        self.sessions.clear()
        for chat in chats:
            item = QListWidgetItem(chat["title"])
            item.setData(Qt.ItemDataRole.UserRole, chat["id"])
            item.setToolTip(chat["title"] + ("\n" + chat["account"] if chat["account"] else ""))
            self.sessions.addItem(item)
            if chat["id"] == selected:
                self.sessions.setCurrentItem(item)
        self.sessions.blockSignals(blocked)
        row = self.db.get("SELECT account_id FROM chats WHERE id=?", selected) if selected is not None else None
        choices(self.account, ((r["name"], r["id"]) for r in self.db.accounts()),
                selected=row["account_id"] if row else self.account.currentData(), first="Общий чат")
        if row and row["account_id"] is None:
            self.account.setCurrentIndex(0)
        self.account.setEnabled(self.chat_id is None and not self.busy)
        self.model.setText(self.db.setting("chat_model", DEFAULT_MODEL))
        configured = bool(self.vault.get(KEY_NAME))
        self.send_button.setEnabled(configured and not self.busy)
        self.new_button.setEnabled(not self.busy)
        self.sessions.setEnabled(not self.busy)
        self.editor.setEnabled(not self.busy)
        self.connection.setText("Сообщения отправляются в OpenAI по нажатию «Отправить»." if configured else
                                "Для ответов добавьте OpenAI API-ключ в настройках чата.")
        self.update_context()
        self.show_messages()

    def update_context(self, *_args):
        self.context.setText("С сообщением будут переданы тематика, аудитория и идеи выбранного аккаунта."
                             if self.account.currentData() is not None else
                             "Общий чат. Для работы над аккаунтом выберите его перед началом новой беседы.")

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
                item.widget().deleteLater()
        rows = self.store.messages(self.chat_id) if self.chat_id is not None else []
        if self.busy and not any(row["state"] == "pending" for row in rows):
            rows.append({"role": "user", "body": self.last_text, "state": "pending", "error": ""})
        if not rows:
            self.messages.addStretch(1)
            self.messages.addWidget(label("Чем помочь с контентом?", "title"))
            self.messages.addWidget(label("Обсудите образ ИИ-модели, подготовьте сценарий "
                                          "или начните с одной из заготовок ниже.", "muted"))
        for message in rows:
            bubble, box = surface("chatUser" if message["role"] == "user" else "chatAssistant")
            header = QHBoxLayout()
            header.addWidget(label("Вы" if message["role"] == "user" else "ИИ-помощник", "section"), 1)
            header.addWidget(button("Копировать", lambda _checked, text=message["body"]:
                                    QGuiApplication.clipboard().setText(text)))
            box.addLayout(header)
            box.addWidget(MessageText(message["body"]) if message["role"] == "assistant" else label(message["body"]))
            if message["state"] == "failed":
                box.addWidget(label(message["error"], "muted"))
            elif message["state"] == "pending":
                box.addWidget(label("Готовлю ответ…", "muted"))
            if message["role"] == "user":
                bubble.setMaximumWidth(700)
                self.messages.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight)
            else:
                self.messages.addWidget(bubble)
        self.messages.addStretch(1)
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(
            self.scroll.verticalScrollBar().maximum()))

    def send(self):
        text = self.editor.toPlainText().strip()
        if self.busy or not text:
            return
        chat_config(self.db, self.vault)
        if self.chat_id is None:
            self.chat_id = self.store.create(self.account.currentData())
            self.drafts.pop(None, None)
        self.last_text = text
        chat_id = self.chat_id
        self.busy = True
        self.worker = Worker("Ответ ИИ-чата", lambda: self.service.send(chat_id, text))
        self.worker.signals.completed.connect(self.finished)
        self.worker.signals.error.connect(self.failed)
        self.progress.show()
        self.refresh()
        self.connection.setText("Готовлю ответ… Можно продолжать работу в других разделах.")
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

    @Slot(str)
    def failed(self, message):
        self.end_request()
        self.refresh()
        self.connection.setText(message)
        self.editor.setPlainText(self.last_text)
