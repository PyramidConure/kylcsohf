from datetime import date, timedelta
from math import ceil

STATUSES = (
    "Не начато", "Настройка профиля", "Исследование ниши", "Подготовка контента",
    "Готово к публикации", "Публикуется", "Требуется внимание",
)
PHASES = (
    ("profile", "Настройка профиля", (
        ("contacts", "Проверить регистрацию и подтверждение контактных данных"),
        ("2fa", "Настроить двухфакторную аутентификацию"),
        ("avatar", "Добавить аватар"),
        ("bio", "Заполнить описание профиля"),
        ("topic", "Определить тематику"),
        ("login", "Проверить безопасность входа"),
    )),
    ("research", "Исследование ниши", (
        ("authors", "Найти интересных авторов выбранной тематики"),
        ("formats", "Изучить популярные форматы Reels"),
        ("audience", "Определить вопросы и интересы аудитории"),
        ("ideas", "Подготовить список идей"),
        ("advantages", "Зафиксировать конкурентные преимущества блога"),
    )),
    ("content", "Подготовка контента", (
        ("reels", "Подготовить первые оригинальные Reels"),
        ("vertical", "Проверить вертикальный формат"),
        ("captions_en", "Подготовить англоязычные описания для аудитории США"),
        ("audio", "Проверить субтитры и звук"),
        ("plan", "Сформировать контент-план"),
        ("professional", "Проверить доступность функций профессионального аккаунта"),
    )),
    ("followup", "Публикации", (
        ("regular", "Начать или продолжить регулярную публикацию контента"),
        ("metrics", "Собрать доступные показатели через официальные API"),
        ("review", "Проанализировать темы и форматы"),
        ("adjust", "Скорректировать контент-план"),
        ("health", "Проверить состояние аккаунта"),
    )),
)
US_ADVICE = (
    "Используйте естественный английский и субтитры. Проверяйте единицы измерения, "
    "локальные примеры и культурный контекст. Планируйте время с учётом выбранного "
    "часового пояса и сравнивайте результаты по фактическим метрикам. "
    "Настройки аудитории не гарантируют показы в США."
)


def calendar(start: date, duration: int, country: str):
    if not 3 <= duration <= 365:
        raise ValueError("Длительность подготовки должна быть от 3 до 365 дней.")
    first = max(1, round(duration * 3 / 14))
    second = max(first + 1, min(duration - 1, round(duration * 7 / 14)))
    windows = ((1, first), (first + 1, second), (second + 1, duration),
               (duration + 1, duration + 7))
    for (phase, label, entries), (lo, hi) in zip(PHASES, windows):
        entries = [(key, title) for key, title in entries if key != "captions_en" or country == "US"]
        for index, (key, title) in enumerate(entries):
            day = lo + ceil(index * (hi - lo) / max(1, len(entries) - 1))
            yield key, phase, label, title, (start + timedelta(days=day - 1)).isoformat()


def preparation_status(account, tasks):
    if account["prep_override"]:
        return account["prep_override"]
    prep = [task for task in tasks if task["phase"] in {"profile", "research", "content"}]
    if not any(task["done"] for task in prep):
        return STATUSES[0]
    return next((task["stage"] for task in prep if not task["done"]), STATUSES[4])
