from datetime import date
import logging

from PySide6.QtCore import QObject, QRunnable, QSize, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .accounts import Accounts, Warmup, task_rows
from .common import Fields, Page, Table, button, choices, label, required, surface, toolbar
from .library import Emulators, Videos
from .operations import Analytics, Posts
from .settings import Settings
from ..db import now

LOG = logging.getLogger("creator_control")


class WorkerSignals(QObject):
    completed = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    def __init__(self, activity, fn):
        super().__init__()
        self.activity = activity
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.fn()
        except (ValueError, OSError) as exc:
            LOG.warning("%s | %s", self.activity, type(exc).__name__)
            self.signals.error.emit(str(exc))
        except Exception as exc:
            LOG.error("%s | %s", self.activity, type(exc).__name__)
            self.signals.error.emit(f"Внутренняя ошибка ({type(exc).__name__}).")
        else:
            LOG.info("%s | завершено", self.activity)
            self.signals.completed.emit(result)


class Overview(Page):
    def __init__(self, window):
        super().__init__(window, "Рабочая сводка по аккаунтам, подготовке и контенту.")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.cards = []
        for title, detail, page in (
            ("Аккаунты", "Профили и подключения", 1), ("Видео", "Файлы в каталоге", 3),
            ("В очереди", "Ожидают отправки", 4), ("Задачи к сроку", "Сегодня и просроченные", 7),
        ):
            card, box = surface()
            link = button(title + "  ›", lambda p=page: self.window.navigation.setCurrentRow(p))
            link.setObjectName("metricLink")
            box.addWidget(link)
            value = label("0", "number")
            box.addWidget(value)
            box.addWidget(label(detail, "muted"))
            self.cards.append(value)
            row.addWidget(card, 1)
        self.layout.addLayout(row)
        content = QHBoxLayout()
        content.setSpacing(12)
        self.layout.addLayout(content, 1)
        tasks = QVBoxLayout()
        tasks.setSpacing(9)
        tasks.addWidget(label("Ближайшие и просроченные задачи", "section"))
        self.table = Table(("Срок", "Состояние", "Аккаунт", "Задача"), status=1,
                           empty="Открытых задач пока нет.")
        self.table.setWordWrap(True)
        self.table.verticalHeader().setDefaultSectionSize(40)
        tasks.addWidget(self.table, 1)
        tasks.addLayout(toolbar(button("Все задачи", lambda: self.window.navigation.setCurrentRow(7))))
        content.addLayout(tasks, 1)
        side = QVBoxLayout()
        side.setSpacing(12)
        next_card, box = surface()
        next_card.setFixedWidth(234)
        box.addWidget(label("СЛЕДУЮЩИЙ ШАГ", "eyebrow"))
        self.next_title = label("", "section")
        self.next_detail = label("", "muted")
        self.next_page = 1
        self.next_action = button("", lambda: self.window.navigation.setCurrentRow(self.next_page), True)
        for item in (self.next_title, self.next_detail, self.next_action):
            box.addWidget(item)
        side.addWidget(next_card)
        quick, box = surface()
        box.addWidget(label("БЫСТРЫЙ ДОСТУП", "eyebrow"))
        for title, page in (("Подготовка Instagram", 2), ("Новая публикация", 4), ("Проверка подключения", 8)):
            link = button(title + "  ›", lambda p=page: self.quick_open(p))
            link.setObjectName("metricLink")
            box.addWidget(link)
        side.addWidget(quick)
        side.addStretch(1)
        content.addLayout(side)

    def quick_open(self, page):
        self.window.navigation.setCurrentRow(page)
        current = self.window.stack.currentWidget()
        if page == 4:
            current.new()
        elif page == 8:
            current.tabs.setCurrentIndex(2)

    def refresh(self):
        tasks = [row for row in self.db.tasks() if not row["done"]]
        posts = self.db.posts()
        today = date.today().isoformat()
        current_time = now()
        values = (len(self.db.accounts()), len(self.db.videos()),
                  sum(p["state"] == "planned" for p in posts),
                  sum(t["due"] <= today for t in tasks))
        for card, value in zip(self.cards, values):
            card.setText(str(value))
        self.table.fill(task_rows(tasks[:20]))
        if not values[0]:
            step = ("Добавьте аккаунт", "Создайте карточку Instagram или YouTube. Для Instagram сразу появится план подготовки.",
                    "Открыть аккаунты", 1)
        elif any(p["state"] in {"committing", "restored"} for p in posts):
            step = ("Сверьте публикации", "Есть записи, результат которых нужно проверить на платформе.",
                    "Открыть очередь", 4)
        elif values[3]:
            step = ("Разберите задачи к сроку", "На сегодня или раньше запланированы незавершённые шаги.",
                    "Открыть задачи", 7)
        elif not values[1]:
            step = ("Добавьте видео", "Импортируйте исходники. FFprobe проверит дорожки и параметры файла.",
                    "Открыть видео", 3)
        elif any(p["state"] == "planned" and p["due"] <= current_time for p in posts):
            step = ("Проверьте очередь", "У некоторых публикаций наступило время отправки.",
                    "Открыть очередь", 4)
        else:
            step = ("Спланируйте выпуск", "Соберите черновик из видео и назначьте дату в очереди.",
                    "Открыть очередь", 4)
        title, detail, action, self.next_page = step
        self.next_title.setText(title)
        self.next_detail.setText(detail)
        self.next_action.setText(action)


class Tasks(Page):
    def __init__(self, window):
        super().__init__(window, "Единый список задач. Просроченные и сегодняшние шаги также видны в обзоре.")
        self.form = Fields()
        self.form.add("account", "Аккаунт", "choice")
        self.form.add("title", "Новая задача")
        self.form.add("due", "Срок", "date")
        self.layout.addWidget(self.form)
        self.layout.addLayout(toolbar(button("Добавить задачу", self.guard(self.add), True),
                                      button("Отметить / вернуть", self.guard(self.complete))))
        self.table = Table(("Срок", "Состояние", "Аккаунт / этап", "Задача"), status=1,
                           empty="Добавьте первую задачу с помощью формы выше.")
        self.layout.addWidget(self.table, 1)

    def refresh(self):
        choices(self.form.widget("account"), ((r["name"], r["id"]) for r in self.db.accounts()), first="Общая задача")
        self.table.fill(task_rows(self.db.tasks()))

    def add(self):
        data = self.form.values()
        self.db.add_task(data["account"], data["title"], data["due"])
        self.form.set({"title": ""})
        self.refresh()

    def complete(self):
        identifier = required(self.table.selected(), "задачу")
        task = self.db.get("SELECT done FROM tasks WHERE id=?", identifier)
        self.db.complete(identifier, not task["done"])
        self.refresh()


PAGES = (
    ("Обзор", Overview), ("Аккаунты", Accounts), ("Прогрев Instagram", Warmup), ("Видео", Videos),
    ("Публикации", Posts), ("Аналитика", Analytics), ("Эмуляторы", Emulators), ("Задачи", Tasks),
    ("Настройки", Settings),
)


class Window(QMainWindow):
    def __init__(self, db, vault):
        super().__init__()
        self.db, self.vault = db, vault
        self.worker = None
        self.after = None
        self.on_error = None
        self.setWindowTitle("Creator Control Center 2.0")
        self.setMinimumSize(1010, 660)
        self.resize(1260, 820)
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(198)
        side_box = QVBoxLayout(side)
        side_box.setContentsMargins(14, 19, 14, 14)
        side_box.setSpacing(10)
        side_box.addWidget(label("Creator", "brand"))
        side_box.addWidget(label("CONTROL CENTER / 2.0", "eyebrow"))
        side_box.addSpacing(8)
        side_box.addWidget(label("РАБОЧАЯ ОБЛАСТЬ", "eyebrow"))
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setAccessibleName("Разделы программы")
        self.navigation.setSpacing(2)
        side_box.addWidget(self.navigation, 1)
        side_box.addWidget(label("ЛОКАЛЬНОЕ ПРИЛОЖЕНИЕ", "eyebrow"))
        outer.addWidget(side)
        work = QWidget()
        work_box = QVBoxLayout(work)
        work_box.setContentsMargins(24, 18, 24, 20)
        work_box.setSpacing(12)
        self.title = label("", "title")
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        self.mode = label("", "mode")
        self.mode.setWordWrap(False)
        top.addWidget(self.mode)
        work_box.addLayout(top)
        self.stack = QStackedWidget()
        work_box.addWidget(self.stack, 1)
        outer.addWidget(work, 1)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)
        for text, cls in PAGES:
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(165, 36))
            self.navigation.addItem(item)
            self.stack.addWidget(cls(self))
        self.navigation.currentRowChanged.connect(self.open_page)
        self.theme()
        self.navigation.setCurrentRow(0)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(lambda: self.guard(self.reminders))
        self.timer.start()

    def reminders(self):
        if self.worker is not None:
            return
        if self.stack.currentIndex() == 0:
            self.stack.currentWidget().refresh()
        overdue = self.db.get("SELECT COUNT(*) AS n FROM tasks WHERE active=1 AND done=0 AND due<=?", date.today().isoformat())["n"]
        due = self.db.get("SELECT COUNT(*) AS n FROM publications WHERE state='planned' AND due<=?",
                          now())["n"]
        if overdue or due:
            self.statusBar().showMessage(f"К выполнению: {overdue} задач · к отправке: {due} публикаций", 8000)

    def open_page(self, index):
        if index >= 0:
            self.stack.setCurrentIndex(index)
            self.title.setText(PAGES[index][0])
            self.guard(self.stack.currentWidget().refresh)

    def new_post(self, video_id):
        self.navigation.setCurrentRow(4)
        self.stack.currentWidget().new(video_id)

    def guard(self, fn):
        try:
            fn()
        except (ValueError, OSError, LookupError) as error:
            self.error(str(error))
        except Exception as error:
            self.error(f"Внутренняя ошибка ({type(error).__name__}).")

    def notice(self, text):
        self.statusBar().showMessage(text, 16000)

    def info(self, title, message):
        box = QMessageBox(QMessageBox.Icon.Information, title, message, parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.exec()

    def error(self, text):
        self.notice(text)
        self.info("Проверка операции", text)

    def confirm(self, title, message):
        box = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        confirm = box.addButton("Подтвердить", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() == confirm

    def job(self, activity, fn, completed, *, error=None):
        if self.worker:
            raise ValueError("Дождитесь завершения текущей операции.")
        LOG.info("%s | начато", activity)
        self.worker = Worker(activity, fn)
        self.after = completed
        self.on_error = error
        self.worker.signals.completed.connect(self.job_done)
        self.worker.signals.error.connect(self.job_error)
        self.navigation.setEnabled(False)
        self.stack.setEnabled(False)
        self.statusBar().showMessage(activity + "…")
        QThreadPool.globalInstance().start(self.worker)

    def clear_job(self):
        self.worker, self.after, self.on_error = None, None, None
        self.navigation.setEnabled(True)
        self.stack.setEnabled(True)
        self.statusBar().clearMessage()

    @Slot(object)
    def job_done(self, result):
        completed = self.after
        self.clear_job()
        self.guard(lambda: completed(result))

    @Slot(str)
    def job_error(self, message):
        callback = self.on_error
        self.clear_job()
        if callback:
            self.guard(callback)
        self.error(message)

    def closeEvent(self, event):
        if self.worker:
            self.notice("Дождитесь завершения операции.")
            event.ignore()
        else:
            super().closeEvent(event)

    def theme(self):
        dark = self.db.setting("theme", "dark") == "dark"
        app = QApplication.instance()
        palette = QPalette()
        base, surface, text, muted, selection = (
            ("#111724", "#1b2434", "#eef3ff", "#acb9d0", "#263753") if dark else
            ("#eff3f9", "#ffffff", "#17243c", "#4b5b73", "#dfebff")
        )
        for role, color in (("Window", base), ("Base", surface), ("AlternateBase", surface),
                            ("Button", surface), ("Text", text), ("WindowText", text),
                            ("ButtonText", text), ("Highlight", selection), ("HighlightedText", text),
                            ("ToolTipBase", surface), ("ToolTipText", text), ("PlaceholderText", muted)):
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
        app.setPalette(palette)
        app.setStyleSheet(stylesheet(dark))
        test_mode = self.db.setting("test_mode", "1") != "0"
        self.mode.setProperty("live", "false" if test_mode else "true")
        self.mode.setText("●  Тестовый режим" if test_mode else "●  Реальные операции")
        self.mode.style().unpolish(self.mode)
        self.mode.style().polish(self.mode)


def stylesheet(dark):
    bg, card, alt, border, text, muted, accent, hover = (
        ("#111724", "#1b2434", "#182031", "#344259", "#eef3ff", "#acb9d0", "#6c96ff", "#263753") if dark else
        ("#eff3f9", "#ffffff", "#f5f8fc", "#cbd6e6", "#17243c", "#4b5b73", "#3765d6", "#dfebff")
    )
    accent_text = "#08162f" if dark else "#ffffff"
    amber = "#F4C887" if dark else "#854700"
    amber_bg = "#392E20" if dark else "#FFF1D7"
    return f"""
    QWidget {{ background:{bg}; color:{text}; font-family:'Segoe UI'; font-size:13px; }}
    #sidebar, #sidebar QLabel, #navigation {{ background:{card}; }}
    #sidebar {{ border-right:1px solid {border}; }}
    #brand {{ font-size:29px; font-weight:750; color:{text}; }}
    #eyebrow {{ color:{muted}; font-size:11px; font-weight:700; }}
    #mode {{ color:{accent}; background:{hover}; border-radius:10px; padding:6px 10px; font-weight:650; }}
    #mode[live="true"] {{ color:{amber}; background:{amber_bg}; }}
    #title {{ font-size:29px; font-weight:700; }}
    #section {{ font-size:15px; font-weight:650; }}
    #number {{ font-size:29px; font-weight:700; color:{accent}; background:{card}; }}
    #muted, #tableEmpty {{ color:{muted}; }}
    #tableEmpty {{ background:transparent; }}
    #card, #card QLabel {{ background:{card}; }}
    #card {{ border:1px solid {border}; border-radius:10px; }}
    #card QCheckBox {{ background:transparent; }}
    #navigation {{ border:0; outline:0; }}
    #navigation::item {{ padding:4px 8px; border-radius:6px; border-left:3px solid transparent; }}
    #navigation::item:hover {{ background:{alt}; }}
    #navigation::item:selected {{ background:{hover}; color:{text}; border-left:3px solid {accent}; }}
    QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox, QTableWidget {{
        background:{card}; border:1px solid {border}; border-radius:6px; padding:5px;
        selection-background-color:{hover}; selection-color:{text}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {{
        border-color:{accent}; }}
    QTableWidget {{ alternate-background-color:{alt}; padding:0; }}
    QTableWidget::item {{ padding:5px 8px; }}
    QHeaderView::section {{ background:{card}; color:{muted}; padding:8px;
        border:0; border-bottom:1px solid {border}; }}
    QProgressBar {{ background:{alt}; border:0; border-radius:4px; min-height:8px; max-height:8px; }}
    QProgressBar::chunk {{ background:{accent}; border-radius:4px; }}
    QPushButton {{ padding:7px 11px; background:{card}; border:1px solid {border}; border-radius:7px; }}
    QPushButton:hover, QToolButton:hover {{ background:{hover}; }}
    QPushButton:focus {{ border-color:{accent}; }}
    QPushButton#primary {{ background:{accent}; color:{accent_text}; border-color:{accent}; font-weight:650; }}
    QPushButton:disabled, QPushButton#primary:disabled {{ color:{muted}; background:{alt}; border-color:{border}; }}
    QPushButton#metricLink {{ text-align:left; font-weight:650; padding:5px 3px; border:1px solid transparent; }}
    QPushButton#metricLink:focus {{ border-color:{accent}; }}
    QPushButton#phase {{ text-align:left; padding:7px 9px; }}
    QPushButton#phase:checked {{ border-color:{accent}; background:{hover}; font-weight:650; }}
    QCheckBox {{ spacing:7px; }}
    QCheckBox::indicator {{ width:14px; height:14px; border:1px solid {muted}; border-radius:4px; background:{card}; }}
    QCheckBox::indicator:checked {{ background:{accent}; border-color:{accent}; }}
    QCheckBox::indicator:focus {{ border-color:{accent}; }}
    QScrollBar:vertical {{ background:{alt}; width:10px; margin:0; }}
    QScrollBar::handle:vertical {{ background:{border}; min-height:24px; border-radius:4px; margin:1px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
    QTabBar::tab {{ padding:7px 12px; background:{card}; }}
    QTabBar::tab:selected {{ background:{hover}; }}
    QCalendarWidget QToolButton {{ color:{text}; }}
    QCalendarWidget QToolButton#qt_calendar_prevmonth, QCalendarWidget QToolButton#qt_calendar_nextmonth {{
        background:{hover}; border-radius:4px; font-size:18px; min-width:21px; }}
    QCalendarWidget QAbstractItemView {{ color:{text}; background:{card}; selection-background-color:{hover}; }}
    QStatusBar {{ color:{muted}; border-top:1px solid {border}; }}
    """
