"""Структура таблицы и проверка диапазонов."""

import re
from typing import Any


HEADERS = ("ID", "Заявитель", "Описание", "Статус")
STATUSES = ("Новый", "В работе", "Закрыт")
MAX_DATA_ROWS = 1000
LAST_ROW = MAX_DATA_ROWS + 1
TABLE_RANGE = f"A1:D{LAST_ROW + 1}"
RequestRows = dict[int, tuple[Any, ...]]


class TableError(ValueError):
    """Данные книги не соответствуют структуре таблицы."""


class RangeError(ValueError):
    """Координаты находятся вне допустимого диапазона."""


def parse_table(data: Any) -> RequestRows:
    """Возвращает непустые строки по их фактическим номерам в Excel."""
    if not isinstance(data, list) or len(data) != LAST_ROW + 1:
        raise TableError("MCP вернул неполный диапазон таблицы.")
    if not all(isinstance(row, list) and len(row) == len(HEADERS) for row in data):
        raise TableError("В диапазоне таблицы должны быть четыре столбца.")
    if tuple(data[0]) != HEADERS:
        raise TableError("Строка 1 должна содержать заголовки ID, Заявитель, Описание, Статус.")
    if any(value is not None for value in data[-1]):
        raise TableError(f"В строке {LAST_ROW + 1} есть данные за границей учебной таблицы.")

    records = {}
    ids = set()
    for number, row in enumerate(data[1:-1], start=2):
        if all(value is None for value in row):
            continue
        request_id, _, _, status = row
        if type(request_id) is not int or request_id <= 0:
            raise TableError(f"В строке {number} ID должен быть положительным целым числом.")
        if request_id in ids:
            raise TableError(f"Повторяющийся ID {request_id} в строке {number}.")
        if status not in STATUSES:
            raise TableError(f"Недопустимый статус в строке {number}.")
        ids.add(request_id)
        records[number] = tuple(row)
    return records


def normalize_cell(value: Any) -> str:
    cell = value.upper() if isinstance(value, str) else ""
    if re.fullmatch(r"[A-D][1-9][0-9]{0,3}", cell) and int(cell[1:]) <= LAST_ROW:
        return cell
    raise RangeError(f"Допустимы ячейки от A1 до D{LAST_ROW}.")


def normalize_range(value: Any) -> str:
    if not isinstance(value, str) or value.count(":") != 1:
        raise RangeError("Укажите диапазон в формате A1:D10.")
    start, end = map(normalize_cell, value.split(":"))
    if start[0] > end[0] or int(start[1:]) > int(end[1:]):
        raise RangeError("Начало диапазона должно предшествовать концу.")
    return f"{start}:{end}"


def is_request_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 32767
        and not value.lstrip().startswith("=")
        and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value)
    )
