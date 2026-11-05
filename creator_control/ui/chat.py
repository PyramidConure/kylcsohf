from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut, QTextDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QProgressBar,
    QScrollArea, QSplitter, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..chat import ChatService, ChatStore, DEFAULT_MODEL, KEY_NAME, chat_config, remember_models, saved_models, validate_model
from .common import Page, Worker, button, choices, label, surface
from .registration import RegistrationPanel


class Composer(QPlainTextEdit):
    submitted = Signal()

    def fit_height(self):
        block, lines = self.document().firstBlock(), 0
        while block.isValid() and lines < 7:
            lines += max(1, block.layout().lineCount())
            block = block.next()
        height = min(102, max(54, lines * self.fontMetrics().lineSpacing() + 16))
        if self.height() != height:
            self.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.fit_height)

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
        last_chat = self.db.setting("chat_last", "")
        self.chat_id = int(last_chat) if last_chat.isdigit() and self.db.get(
            "SELECT id FROM chats WHERE id=?", int(last_chat)) else None
        self.draft_model = self.db.setting("chat_model", DEFAULT_MODEL)
        self.busy = False
        self.request_kind = None
        self.worker = None
        self.draft_account = self.store.draft(None)["account_id"]
        self.auto_scroll = True
        self.rendered = None
        self.configured = False
        self.preparation_open = self.db.setting("chat_tools_visible", "0") == "1"
        self.last_text = ""
        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)
        self.layout.addWidget(self.split, 1)
        self.rail, rail_box = surface("chatRail")
        self.rail.setMinimumWidth(165)
        self.rail.setMaximumWidth(245)
        self.new_button = button("＋  Новая беседа", self.guard(self.new_chat), True)
        rail_box.addWidget(self.new_button)
        rail_box.addWidget(button("Аккаунты и материалы", self.guard(lambda: self.show_registration(True))))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск в чатах")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_history)
        rail_box.addWidget(self.search)
        rail_box.addWidget(label("ИСТОРИЯ", "eyebrow"))
        self.sessions = QListWidget()
        self.sessions.setObjectName("chatHistory")
        self.sessions.setWordWrap(True)
        self.sessions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        self.export_button = button("Экспорт", self.guard(self.export_chat))
        top.addWidget(self.export_button)
        top.addWidget(button("Настройки", self.window.open_chat_settings))
        center.addLayout(top)
        heading = QHBoxLayout()
        self.thread_title = label("Новая беседа", "section")
        heading.addWidget(self.thread_title, 1)
        self.latest_button = button("Вниз ↓", self.to_latest)
        self.latest_button.hide()
        heading.addWidget(self.latest_button)
        center.addLayout(heading)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(self.adjust_scroll)
        bar.sliderMoved.connect(lambda _value: self.user_scrolled())
        bar.actionTriggered.connect(lambda _action: QTimer.singleShot(0, self.user_scrolled))
        bar.valueChanged.connect(lambda _value: self.update_latest_button())
        self.canvas = QWidget()
        self.canvas.setObjectName("chatCanvas")
        self.messages = QVBoxLayout(self.canvas)
        self.messages.setContentsMargins(12, 12, 12, 12)
        self.messages.setSpacing(16)
        self.scroll.setWidget(self.canvas)
        self.contents = QStackedWidget()
        self.contents.addWidget(self.scroll)
        self.registration = RegistrationPanel(window)
        self.registration.layout.insertWidget(0, button("← Вернуться в переписку", lambda: self.show_registration(False)))
        self.contents.addWidget(self.registration)
        center.addWidget(self.contents, 1)
        self.connection = label("", "muted")
        self.configure_button = button("Подключить OpenAI", self.window.open_chat_settings)
        self.chat_notice = QWidget()
        notice = QHBoxLayout(self.chat_notice)
        notice.setContentsMargins(0, 0, 0, 0)
        notice.addWidget(self.connection, 1)
        notice.addWidget(self.configure_button)
        center.addWidget(self.chat_notice)
        self.composer, box = surface("chatComposer")
        self.editor = Composer()
        self.editor.setObjectName("chatInput")
        self.editor.setPlaceholderText("Сообщение…")
        self.editor.setMinimumHeight(54)
        self.editor.setMaximumHeight(102)
        self.editor.setAccessibleName("Сообщение ИИ-чату")
        self.editor.submitted.connect(self.guard(self.send))
        box.addWidget(self.editor)
        footer = QHBoxLayout()
        self.plus_button = button("＋", self.show_tools)
        self.plus_button.setFixedWidth(34)
        self.plus_button.setAccessibleName("Агенты и инструменты чата")
        self.plus_button.setToolTip("Агент подготовки аккаунтов и материалы")
        footer.addWidget(self.plus_button)
        self.account = QComboBox()
        self.account.setMinimumContentsLength(11)
        self.account.setMaximumWidth(225)
        self.account.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.account.setAccessibleName("Контекст аккаунта")
        self.account.currentIndexChanged.connect(self.update_context)
        footer.addWidget(self.account)
        self.next_button = button("Следующий шаг", self.preparation_prompt)
        self.next_button.setToolTip("Подготовить вопрос о задачах выбранного аккаунта")
        footer.addWidget(self.next_button)
        footer.addStretch(1)
        self.counter = label("", "chatFootnote")
        self.counter.setWordWrap(False)
        footer.addWidget(self.counter)
        self.send_button = button("↑", self.guard(self.send), True)
        self.send_button.setFixedSize(39, 37)
        self.send_button.setAccessibleName("Отправить сообщение")
        self.send_button.setToolTip("Отправить · Enter")
        footer.addWidget(self.send_button)
        box.addLayout(footer)
        center.addWidget(self.composer)
        self.context = label("", "chatFootnote")
        center.addWidget(self.context)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        center.addWidget(self.progress)
        self.split.addWidget(work)
        self.split.setSizes([195, 765])
        self.draft_timer = QTimer(self)
        self.draft_timer.setSingleShot(True)
        self.draft_timer.setInterval(400)
        self.draft_timer.timeout.connect(self.guard(self.flush_draft))
        self.editor.textChanged.connect(self.editor_changed)
        self.account.currentIndexChanged.connect(self.schedule_draft)
        self.editor.setPlainText(self.store.draft(self.chat_id)["body"])
        self.shortcuts = []
        for keys, callback in (("Ctrl+N", self.guard(self.new_chat)), ("Ctrl+F", self.search.setFocus)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)

    def show_tools(self):
        menu = QMenu(self.plus_button)
        agent = menu.addAction("Агент подготовки и регистрации…")
        materials = menu.addAction("Материалы выбранного аккаунта…")
        menu.addSeparator()
        new = menu.addAction("Новая беседа")
        selected = menu.exec(self.plus_button.mapToGlobal(self.plus_button.rect().topLeft()))
        if selected in (agent, materials):
            self.show_registration(True, self.account.currentData())
        elif selected == new:
            self.window.guard(self.new_chat)

    def show_registration(self, visible=True, account_id=None):
        self.flush_draft()
        self.preparation_open = visible
        self.db.settings({"chat_tools_visible": "1" if visible else "0"})
        if visible:
            self.registration.refresh(account_id or self.account.currentData())
        self.apply_mode()

    def apply_mode(self):
        self.contents.setCurrentWidget(self.registration if self.preparation_open else self.scroll)
        self.rail.setVisible(not self.preparation_open and self.history_toggle.isChecked())
        self.composer.setVisible(not self.preparation_open)
        self.context.setVisible(not self.preparation_open)
        self.chat_notice.setVisible(not self.preparation_open)
        self.thread_title.setVisible(not self.preparation_open)
        self.latest_button.setVisible(not self.preparation_open and
                                      self.scroll.verticalScrollBar().maximum() - self.scroll.verticalScrollBar().value() > 24)

    def toggle_history(self):
        if self.preparation_open:
            self.show_registration(False)
        self.rail.setVisible(self.history_toggle.isChecked())
        self.db.settings({"chat_history_visible": "1" if self.history_toggle.isChecked() else "0"})

    def filter_history(self, *_args):
        matches = self.store.matching_chats(self.search.text())
        for index in range(self.sessions.count()):
            item = self.sessions.item(index)
            item.setHidden(item.data(Qt.ItemDataRole.UserRole) not in matches)

    def schedule_draft(self, *_args):
        self.draft_timer.start()

    def editor_changed(self):
        self.schedule_draft()
        self.update_composer()

    def update_composer(self):
        length = len(self.editor.toPlainText())
        self.counter.setText(f"{length:,} / 16 000".replace(",", " ") if length else "")
        local_command = self.editor.toPlainText().strip().casefold() in {
            "/аккаунты", "/регистрация", "/материалы", "/accounts"}
        self.send_button.setEnabled((self.configured or local_command) and not self.busy and
                                    0 < len(self.editor.toPlainText().strip()) <= 16_000)
        self.editor.fit_height()

    def flush_draft(self):
        self.draft_timer.stop()
        if self.db.path.exists():
            self.store.save_draft(self.chat_id, self.editor.toPlainText(),
                                  self.draft_account if self.chat_id is None else None)
            self.db.settings({"chat_last": str(self.chat_id or "")})

    def to_latest(self):
        self.auto_scroll = True
        self.adjust_scroll()

    def adjust_scroll(self, *_args):
        if self.auto_scroll:
            self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum())
        self.update_latest_button()

    def user_scrolled(self):
        bar = self.scroll.verticalScrollBar()
        self.auto_scroll = bar.maximum() - bar.value() <= 24
        self.update_latest_button()

    def update_latest_button(self):
        bar = self.scroll.verticalScrollBar()
        self.latest_button.setVisible(bar.maximum() - bar.value() > 24)

    def preparation_prompt(self):
        if self.account.currentData() is not None:
            self.use_prompt("Посмотри фактический прогресс подготовки выбранного аккаунта. "
                            "Предложи три следующих выполнимых шага и подготовь нужные тексты. "
                            "Отдели готовые материалы от действий, которые владельцу ещё нужно выполнить.")

    def export_chat(self):
        if self.chat_id is None:
            raise ValueError("Сначала выберите сохранённую беседу.")
        text = self.store.export_markdown(self.chat_id)
        default = self.window.data_folder("chats") / f"chat-{self.chat_id}-{datetime.now():%Y%m%d-%H%M%S}.md"
        path, _filter = QFileDialog.getSaveFileName(self, "Экспорт беседы", str(default), "Markdown (*.md)")
        if path:
            with Path(path).open("x", encoding="utf-8") as target:
                target.write(text)
            self.window.notice("Беседа сохранена: " + path)

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
                selected=row["account_id"] if row else self.draft_account,
                first="Без аккаунта")
        if row and row["account_id"] is None:
            self.account.setCurrentIndex(0)
        self.account.setEnabled(self.chat_id is None and not self.busy)
        selected_model = (row["model"] or self.db.setting("chat_model", DEFAULT_MODEL)) if row else self.draft_model
        models = list(dict.fromkeys([*saved_models(self.db), selected_model]))
        choices(self.model, ((model, model) for model in models), selected=selected_model)
        self.model.setEnabled(not self.busy)
        configured = self.configured = bool(self.vault.get(KEY_NAME))
        self.update_composer()
        self.catalog_button.setEnabled(configured and not self.busy)
        self.new_button.setEnabled(not self.busy)
        self.sessions.setEnabled(not self.busy)
        self.editor.setEnabled(not self.busy)
        self.export_button.setEnabled(self.chat_id is not None and not self.busy)
        self.history_toggle.setChecked(self.db.setting("chat_history_visible", "1") != "0")
        self.rail.setVisible(self.history_toggle.isChecked())
        self.connection.setText("" if configured else "Для ответов нужен API-ключ. /аккаунты работает без ключа.")
        self.connection.setVisible(not configured)
        self.configure_button.setVisible(not configured)
        self.update_context()
        self.show_messages()
        if self.preparation_open:
            self.registration.refresh()
        self.apply_mode()

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
                             (" · Учтены аккаунт и задачи подготовки" if with_account else ""))
        self.next_button.setVisible(with_account)
        self.next_button.setEnabled(not self.busy)
        if self.chat_id is None:
            self.draft_account = self.account.currentData()

    def use_prompt(self, text):
        if not self.busy:
            self.editor.setPlainText(text)
            self.editor.setFocus()

    def new_chat(self):
        if self.busy:
            raise ValueError("Дождитесь ответа.")
        self.flush_draft()
        self.chat_id = None
        self.draft_model = self.db.setting("chat_model", DEFAULT_MODEL)
        draft = self.store.draft(None)
        self.draft_account = draft["account_id"]
        self.editor.setPlainText(draft["body"])
        self.db.settings({"chat_last": ""})
        self.refresh()
        self.editor.setFocus()

    def open_account(self, account_id):
        if self.busy:
            raise ValueError("Дождитесь текущего ответа.")
        account = self.db.account(account_id)
        if account is None:
            raise ValueError("Аккаунт не найден.")
        self.flush_draft()
        self.preparation_open = False
        self.db.settings({"chat_tools_visible": "0"})
        chat = self.db.get("SELECT id FROM chats WHERE account_id=? ORDER BY updated DESC,id DESC", account_id)
        self.chat_id = chat["id"] if chat else self.store.create(account_id)
        if chat is None:
            self.store.rename(self.chat_id, "Подготовка · " + account["name"])
        self.editor.setPlainText(self.store.draft(self.chat_id)["body"])
        self.db.settings({"chat_last": str(self.chat_id)})
        self.refresh()
        if not self.editor.toPlainText() and not self.store.messages(self.chat_id):
            self.preparation_prompt()
        self.editor.setFocus()

    def select_chat(self):
        item = self.sessions.currentItem()
        if item is None:
            return
        selected = item.data(Qt.ItemDataRole.UserRole)
        if selected == self.chat_id:
            return
        self.flush_draft()
        self.chat_id = selected
        self.editor.setPlainText(self.store.draft(selected)["body"])
        self.db.settings({"chat_last": str(selected)})
        self.refresh()

    def show_messages(self):
        rows = self.store.messages(self.chat_id) if self.chat_id is not None else []
        if self.busy and self.request_kind == "message" and not any(row["state"] == "pending" for row in rows):
            rows.append({"role": "user", "body": self.last_text, "state": "pending", "error": "", "model": ""})
        signature = (self.chat_id, [(row.get("id"), row["state"], row["body"], row.get("model")) for row in rows])
        if signature == self.rendered:
            return
        if self.rendered is None or self.rendered[0] != self.chat_id:
            self.auto_scroll = True
        self.rendered = signature
        while self.messages.count():
            item = self.messages.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
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
                retry = button("В поле ввода", lambda _checked, text=message["body"]: self.use_prompt(text))
                retry.setObjectName("chatCopy")
                footer.addWidget(retry)
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
        QTimer.singleShot(0, self.adjust_scroll)

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
        if text.casefold() in {"/аккаунты", "/регистрация", "/материалы", "/accounts"}:
            self.editor.clear()
            self.flush_draft()
            self.show_registration(True)
            return
        chat_config(self.db, self.vault)
        self.select_model()
        model = validate_model(self.model.currentText().strip())
        if self.chat_id is None:
            self.store.save_draft(None, "")
            self.chat_id = self.store.create(self.account.currentData(), model)
        self.auto_scroll = True
        self.flush_draft()
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
        self.flush_draft()
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
