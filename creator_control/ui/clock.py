from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QVBoxLayout, QWidget

from .common import choices, label

CITIES = {
    "moscow": ("Москва", "Europe/Moscow"),
    "petersburg": ("Питер", "Europe/Moscow"),
    "irkutsk": ("Иркутск", "Asia/Irkutsk"),
    "ulan_ude": ("Улан-Удэ", "Asia/Irkutsk"),
}


class Clock(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.setObjectName("topClock")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(9)
        values = QVBoxLayout()
        values.setSpacing(0)
        self.time = label("", "clockTime")
        self.time.setWordWrap(False)
        self.date = label("", "muted")
        self.date.setWordWrap(False)
        values.addWidget(self.time)
        values.addWidget(self.date)
        layout.addLayout(values)
        self.city = QComboBox()
        self.city.setAccessibleName("Часовой пояс верхней панели")
        self.city.setMinimumWidth(105)
        choices(self.city, ((name, key) for key, (name, _zone) in CITIES.items()),
                selected=db.setting("clock_city", "moscow"))
        self.city.currentIndexChanged.connect(self.change_city)
        layout.addWidget(self.city)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start()
        self.update_clock()

    def change_city(self, _index):
        self.db.settings({"clock_city": self.city.currentData()})
        self.update_clock()

    def update_clock(self, moment=None):
        name, zone = CITIES[self.city.currentData()]
        local = (moment or datetime.now(UTC)).astimezone(ZoneInfo(zone))
        self.time.setText(local.strftime("%H:%M:%S"))
        self.date.setText(local.strftime("%d.%m.%Y"))
        self.setToolTip(f"{name} · {zone} · UTC{local:%z}")
