from PySide6.QtCore import QDate, QDateTime, QLocale, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView, QCalendarWidget, QCheckBox, QComboBox, QDateEdit, QDateTimeEdit,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)


def label(text, style=None):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.TextFormat.PlainText)
    if style:
        item.setObjectName(style)
    return item


def button(text, callback, primary=False):
    item = QPushButton(text)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    item.clicked.connect(callback)
    if primary:
        item.setObjectName("primary")
    return item


def toolbar(*widgets):
    row = QHBoxLayout()
    row.setSpacing(8)
    for item in widgets:
        row.addWidget(item)
    row.addStretch(1)
    return row


def surface(name="card"):
    frame = QFrame()
    frame.setObjectName(name)
    box = QVBoxLayout(frame)
    box.setContentsMargins(14, 12, 14, 12)
    box.setSpacing(8)
    return frame, box


class StatusDelegate(QStyledItemDelegate):
    colors = {
        "good": ("#8DDEB4", "#166548"),
        "action": ("#F4C887", "#854700"),
        "error": ("#FFADB5", "#982836"),
        "neutral": ("#A7C4FF", "#244FAD"),
    }
    tones = {
        "Готово": "good", "Обработано платформой": "good", "Android готов": "good",
        "Просрочено": "error", "Отклонено": "error",
        "Сегодня": "action", "Проверить результат": "action", "Сверить после восстановления": "action",
        "Можно продолжить": "action", "Токен истёк": "action",
    }

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setWidth(size.width() + 31)
        return size

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        tone = self.tones.get(styled.text, "neutral")
        dark = styled.palette.color(QPalette.ColorRole.Base).lightness() < 128
        color = QColor(self.colors[tone][0 if dark else 1])
        styled.palette.setColor(QPalette.ColorRole.Text, color)
        styled.palette.setColor(QPalette.ColorRole.HighlightedText, color)
        styled.font.setWeight(QFont.Weight.DemiBold)
        styled.text = "●  " + styled.text
        styled.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget)


class Table(QTableWidget):
    def __init__(self, headers, *, status=None, stretch=None, empty=""):
        super().__init__(0, len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(34)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.stretch = len(headers) - 1 if stretch is None else stretch
        header.setSectionResizeMode(self.stretch, QHeaderView.ResizeMode.Stretch)
        if status is not None:
            self.setItemDelegateForColumn(status, StatusDelegate(self))
        self.status = status
        self.setWordWrap(False)
        self.empty = label(empty, "tableEmpty")
        self.empty.setParent(self.viewport())
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.update_empty()

    def empty_text(self, message):
        self.empty.setText(message)
        self.update_empty()

    def update_empty(self):
        self.empty.setGeometry(self.viewport().rect().adjusted(14, 8, -14, -8))
        self.empty.setVisible(self.rowCount() == 0 and bool(self.empty.text()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "empty"):
            self.update_empty()

    def selected(self):
        item = self.item(self.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item and item.isSelected() else None

    def fill(self, rows, selected=None):
        if selected is None:
            selected = self.selected()
        blocked = self.blockSignals(True)
        rows = list(rows)
        self.setRowCount(0)
        self.setRowCount(len(rows))
        active = None
        for i, (identifier, values) in enumerate(rows):
            for column, value in enumerate(values):
                text = "Недоступно" if value is None else str(value)
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if not column:
                    item.setData(Qt.ItemDataRole.UserRole, identifier)
                self.setItem(i, column, item)
            if identifier == selected:
                active = i
        for index in range(self.columnCount()):
            if index == self.stretch:
                continue
            self.resizeColumnToContents(index)
            self.setColumnWidth(index, min(215, max(107 if index == self.status else 86, self.columnWidth(index))))
        if active is not None:
            self.selectRow(active)
        self.blockSignals(blocked)
        self.update_empty()


def choices(combo, pairs, selected=None, *, first=None):
    previous = combo.currentData() if selected is None else selected
    blocked = combo.blockSignals(True)
    combo.clear()
    if first is not None:
        combo.addItem(first, None)
    for name, value in pairs:
        combo.addItem(str(name), value)
    index = combo.findData(previous)
    combo.setCurrentIndex(max(0, index))
    combo.blockSignals(blocked)


def calendar_widget():
    calendar = QCalendarWidget()
    calendar.setLocale(QLocale(QLocale.Language.Russian))
    calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    for weekday in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
        calendar.setWeekdayTextFormat(weekday, QTextCharFormat())
    for name, arrow, description in (("prev", "‹", "Предыдущий месяц"), ("next", "›", "Следующий месяц")):
        control = calendar.findChild(QToolButton, f"qt_calendar_{name}month")
        if control:
            control.setArrowType(Qt.ArrowType.NoArrow)
            control.setIcon(QIcon())
            control.setText(arrow)
            control.setToolTip(description)
            control.setAccessibleName(description)
    return calendar


def required(selection, description):
    if selection is None:
        raise ValueError("Выберите " + description + ".")
    return selection


class Fields(QWidget):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.form = QFormLayout(self)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(12)
        self.form.setVerticalSpacing(7)

    def add(self, key, title, kind="line", options=()):
        if kind == "choice":
            widget = QComboBox()
            widget.setMinimumContentsLength(11)
            widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            choices(widget, options)
        elif kind == "date":
            widget = QDateEdit(QDate.currentDate())
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("dd.MM.yyyy")
        elif kind == "datetime":
            widget = QDateTimeEdit(QDateTime.currentDateTime())
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("dd.MM.yyyy HH:mm")
        elif kind == "int":
            widget = QSpinBox()
            widget.setRange(*options)
        elif kind == "text":
            widget = QPlainTextEdit()
            widget.setMaximumHeight(76)
        elif kind == "bool":
            widget = QCheckBox()
        else:
            widget = QLineEdit()
            if kind == "secret":
                widget.setEchoMode(QLineEdit.EchoMode.Password)
        self.fields[key] = (widget, kind)
        self.form.addRow(title, widget)
        return widget

    def widget(self, key):
        return self.fields[key][0]

    def show_field(self, key, visible):
        self.form.setRowVisible(self.widget(key), visible)

    def set(self, values):
        for key, value in values.items():
            widget, kind = self.fields[key]
            if kind == "choice":
                widget.setCurrentIndex(max(0, widget.findData(value)))
            elif kind == "date":
                widget.setDate(QDate.fromString(value, "yyyy-MM-dd"))
            elif kind == "datetime":
                widget.setDateTime(QDateTime.fromString(value, "yyyy-MM-dd HH:mm"))
            elif kind == "int":
                widget.setValue(value)
            elif kind == "text":
                widget.setPlainText(value or "")
            elif kind == "bool":
                widget.setChecked(bool(value))
            else:
                widget.setText(str(value) if value is not None else "")

    def values(self):
        result = {}
        for key, (widget, kind) in self.fields.items():
            if kind == "choice":
                result[key] = widget.currentData()
            elif kind == "date":
                result[key] = widget.date().toString("yyyy-MM-dd")
            elif kind == "datetime":
                result[key] = widget.dateTime().toString("yyyy-MM-dd HH:mm")
            elif kind == "int":
                result[key] = widget.value()
            elif kind == "text":
                result[key] = widget.toPlainText().strip()
            elif kind == "bool":
                result[key] = widget.isChecked()
            else:
                result[key] = widget.text().strip()
        return result


def scroll(widget):
    wrapper = QScrollArea()
    wrapper.setWidgetResizable(True)
    wrapper.setFrameShape(QScrollArea.Shape.NoFrame)
    wrapper.setWidget(widget)
    return wrapper


class Page(QWidget):
    def __init__(self, window, subtitle):
        super().__init__()
        self.window, self.db, self.vault = window, window.db, window.vault
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(12)
        self.layout.addWidget(label(subtitle, "muted"))

    def guard(self, fn):
        def wrapped(*_):
            self.window.guard(fn)
        return wrapped

    def refresh(self):
        pass
