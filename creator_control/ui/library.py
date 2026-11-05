import json
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QSplitter, QVBoxLayout, QWidget

from ..tools import LDPlayer, VIDEO_EXTENSIONS, import_video, sha256, vertical
from .common import Fields, Page, Table, button, label, required, toolbar


def describe(metadata):
    seconds = metadata.get("seconds")
    width, height = metadata.get("width"), metadata.get("height")
    return (f"{width or '—'} × {height or '—'} · "
            f"{f'{seconds:.2f} с' if seconds is not None else 'длительность недоступна'} · "
            f"{metadata.get('video') or '—'} / {metadata.get('audio') or 'без аудиодорожки'}")


class Videos(Page):
    def __init__(self, window):
        super().__init__(window, "Каталог хранит ссылки на локальные файлы. FFprobe проверяет дорожки, а SHA-256 находит дубликаты.")
        self.layout.addLayout(toolbar(button("Импортировать файлы", self.guard(self.add), True),
                                      button("Предпросмотр", self.guard(self.preview)),
                                      button("Подготовить MP4 9:16", self.guard(self.convert))))
        split = QSplitter()
        self.layout.addWidget(split, 1)
        self.table = Table(("Видео", "Параметры"), empty="Нажмите «Импортировать файлы», чтобы заполнить каталог.")
        self.table.itemSelectionChanged.connect(self.guard(self.load))
        split.addWidget(self.table)
        panel = QWidget()
        right = QVBoxLayout(panel)
        self.details = label("Выберите видео.", "section")
        right.addWidget(self.details)
        self.form = Fields()
        self.form.add("title", "Название")
        self.form.add("description", "Описание", "text")
        right.addWidget(self.form)
        right.addLayout(toolbar(button("Сохранить", self.guard(self.save), True),
                                button("В очередь", self.guard(self.enqueue))))
        right.addLayout(toolbar(button("Указать новый путь…", self.guard(self.relocate))))
        right.addStretch(1)
        split.addWidget(panel)
        split.setSizes([620, 380])

    def refresh(self):
        self.table.fill((row["id"], (row["title"], describe(json.loads(row["metadata"])))) for row in self.db.videos())
        self.load()

    def video(self):
        return self.db.get("SELECT * FROM videos WHERE id=?", required(self.table.selected(), "видео"))

    def load(self):
        if self.table.selected() is None:
            return
        row = self.video()
        self.details.setText(row["path"] + "\n" + describe(json.loads(row["metadata"])))
        self.form.set({key: row[key] for key in self.form.fields})

    def add(self):
        formats = " ".join("*" + suffix for suffix in sorted(VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Импорт видео", str(self.window.data_folder("media")),
                                               f"Видео ({formats})")
        if not files:
            return
        directory = self.db.setting("ffmpeg")

        def batch():
            added = 0
            errors = []
            for path in files:
                try:
                    import_video(self.db, path, directory)
                    added += 1
                except (OSError, ValueError) as exc:
                    errors.append(f"{Path(path).name} — {exc}")
            return added, errors

        def finish(result):
            self.refresh()
            added, errors = result
            self.window.notice(f"Импортировано: {added} из {len(files)}.")
            if errors:
                self.window.info("Результат импорта", "\n".join(errors[:15]))

        self.window.job("Проверка видео и поиск дубликатов", batch, finish)

    def preview(self):
        path = self.video()["path"]
        if Path(path).suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("Выберите видео с поддерживаемым расширением.")
        os.startfile(path)

    def save(self):
        video = self.video()
        self.db.save_video(video["id"], **self.form.values())
        self.refresh()
        self.window.notice("Название и описание сохранены.")

    def relocate(self):
        video = self.video()
        formats = " ".join("*" + suffix for suffix in sorted(VIDEO_EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(self, "Укажите перемещённое видео",
                                             str(self.window.data_folder("media")), f"Видео ({formats})")
        if not path:
            return
        if Path(path).suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("Выберите видео с поддерживаемым расширением.")
        self.window.job("Проверка файла по SHA-256",
                        lambda: self.db.relocate_video(video["id"], path, sha256(path)),
                        lambda _result: self.converted("Путь обновлён. Записи очереди сохранены."))

    def convert(self):
        source = self.video()["path"]
        file, _ = QFileDialog.getSaveFileName(self, "Новый MP4 9:16",
                                             str(self.window.data_folder("media") / (Path(source).stem + "-9x16.mp4")),
                                             "Видео MP4 (*.mp4)")
        if not file:
            return
        directory = self.db.setting("ffmpeg")

        def convert_and_import():
            metadata = vertical(source, file, directory)
            try:
                self.db.add_video(file, sha256(file), metadata)
                return f"Готово: {describe(metadata)}"
            except (OSError, ValueError) as exc:
                return f"Файл создан. Каталог: {exc}"

        self.window.job("FFmpeg готовит вертикальную копию", convert_and_import, self.converted)

    def converted(self, message):
        self.refresh()
        self.window.notice(message)

    def enqueue(self):
        self.window.new_post(self.video()["id"])


class Emulators(Page):
    def __init__(self, window):
        super().__init__(window, "LDPlayer и локальный ADB для ручной работы и диагностики выбранного экземпляра.")
        self.instances = {}
        self.layout.addLayout(toolbar(
            button("Обнаружить", self.guard(self.discover), True),
            button("Запустить", self.guard(lambda: self.action("launch"))),
            button("Остановить", self.guard(lambda: self.action("quit"))),
            button("Открыть Instagram", self.guard(lambda: self.action("instagram"))),
        ))
        self.layout.addLayout(toolbar(button("Диагностика", self.guard(self.diagnose)),
                                      button("Снимок PNG", self.guard(self.screenshot))))
        self.table = Table(("Индекс", "Экземпляр", "Android", "PID LDPlayer", "PID виртуальной машины"),
                           status=2, stretch=1, empty="Нажмите «Обнаружить», чтобы получить список LDPlayer.")
        self.layout.addWidget(self.table, 1)
        self.details = label("Выберите папку LDPlayer в настройках и нажмите «Обнаружить».", "section")
        self.layout.addWidget(self.details)

    def discover(self):
        directory = self.db.setting("ldplayer")
        self.window.job("Чтение списка LDPlayer", lambda: LDPlayer(directory).instances(), self.show_instances)

    def show_instances(self, instances):
        self.instances = {instance.index: instance for instance in instances}
        self.table.empty_text("В указанной папке LDPlayer экземпляры не найдены.")
        self.table.fill((i.index, (i.index, i.name, i.status, i.player_pid, i.vm_pid)) for i in instances)
        self.details.setText(f"Найдено экземпляров: {len(instances)}")

    def selected(self):
        return self.instances[required(self.table.selected(), "экземпляр LDPlayer")]

    def action(self, action):
        instance = self.selected()
        if action == "quit" and not self.window.confirm("Остановить LDPlayer", f"Остановить «{instance.name}»?"):
            return
        directory = self.db.setting("ldplayer")

        def execute():
            driver = LDPlayer(directory)
            driver.action(instance.index, action)
            return driver.instances()

        self.window.job("Выполнение команды LDPlayer", execute, self.show_instances)

    def diagnose(self):
        index = self.selected().index
        directory = self.db.setting("ldplayer")

        def diagnose_current():
            driver = LDPlayer(directory)
            instance = next((row for row in driver.instances() if row.index == index), None)
            return driver.diagnose(required(instance, "существующий экземпляр LDPlayer"))

        self.window.job("Проверка ADB, Instagram и ресурсов", diagnose_current, self.show_diagnostic)

    def show_diagnostic(self, data):
        cpu = "—" if data["cpu"] is None else f"{data['cpu']}%"
        ram = "—" if data["ram"] is None else f"{data['ram']} МБ"
        boot = "Готов" if data["boot"] == "1" else "Ожидание или недоступен"
        ig = "Установлен" if data["instagram"] else "Отсутствует или не удалось проверить"
        note = data.get("note", "")
        self.details.setText(f"{data['status']} · ADB {data['adb']} · загрузка Android: {boot}\n"
                             f"Instagram: {ig} · CPU {cpu} · RAM {ram}\n{note}")

    def screenshot(self):
        index = self.selected().index
        name = f"ldplayer-{index}-{datetime.now():%Y%m%d-%H%M%S}.png"
        file, _ = QFileDialog.getSaveFileName(self, "Сохранить снимок тестовой сессии",
                                             str(self.window.data_folder("screenshots") / name),
                                             "Снимок PNG (*.png)")
        if not file:
            return
        directory = self.db.setting("ldplayer")

        def capture():
            driver = LDPlayer(directory)
            instance = next((row for row in driver.instances() if row.index == index), None)
            driver.screenshot(required(instance, "существующий экземпляр LDPlayer"), file)

        self.window.job("Снимок экрана выбранного LDPlayer", capture,
                        lambda _result: self.window.notice(f"Снимок сохранён: {file}"))
