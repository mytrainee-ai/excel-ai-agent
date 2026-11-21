import pytest

from services.validation import HEADERS, LAST_ROW, TableError, parse_table


def test_parse_table_keeps_row_numbers_across_gaps():
    rows = [[None] * 4 for _ in range(LAST_ROW + 1)]
    rows[0] = list(HEADERS)
    rows[1] = [101, "Иван", "Принтер", "Новый"]
    rows[LAST_ROW - 1] = [150, "Анна", "Ноутбук", "В работе"]

    records = parse_table(rows)

    assert list(records) == [2, LAST_ROW]
    assert records[LAST_ROW][0] == 150


@pytest.mark.parametrize(
    ("row", "values", "message"),
    [
        (1, ["Номер", "Заявитель", "Описание", "Статус"], "заголовки"),
        (3, [101, "Анна", "Ноутбук", "Новый"], "Повторяющийся ID"),
        (3, [True, "Анна", "Ноутбук", "Новый"], "целым числом"),
        (3, [0, "Анна", "Ноутбук", "Новый"], "целым числом"),
        (3, [103, "Анна", "Ноутбук", "Удалён"], "Недопустимый статус"),
        (LAST_ROW + 1, [105, "Анна", "Ноутбук", "Новый"], "за границей"),
    ],
)
def test_parse_table_rejects_bad_structure(row, values, message):
    rows = [[None] * 4 for _ in range(LAST_ROW + 1)]
    rows[0] = list(HEADERS)
    rows[1] = [101, "Иван", "Принтер", "Новый"]
    rows[row - 1] = values

    with pytest.raises(TableError, match=message):
        parse_table(rows)
