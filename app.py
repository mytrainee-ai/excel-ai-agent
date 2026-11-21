"""Страница Streamlit для работы с заявками."""

import asyncio
import re

import streamlit as st

from agent.agent import AgentReply, run_turn
from config.settings import Settings, SettingsError, load_settings
from mcp_client.client import McpConnectionError
from services.confirmations import PendingConfirmation, confirm_status, confirmation_text
from services.operation_log import JournalEntry, OperationStatus
from services.requests_service import DeniedCall, RequestsController, RequestsError, filter_records
from services.validation import HEADERS, TableError


_OPERATION_LABELS = {
    "completed": "Завершено",
    "error": "Ошибка",
    "pending": "Ожидает подтверждения",
    "cancelled": "Отменено",
    "recorded": "Вызовы сохранены",
}
_CALL_LABELS = {
    "success": "успех",
    "tool_error": "ошибка инструмента",
    "invalid_response": "некорректный ответ",
    "timeout": "тайм-аут",
    "request_error": "ошибка запроса",
    "cancelled": "вызов прерван",
}


async def _refresh_table(settings: Settings) -> None:
    operation = None
    status: OperationStatus = "error"
    outcome = "Обновление таблицы прервано."
    try:
        async with RequestsController(settings).operation() as operation:
            records = await operation.read_table()
    except (McpConnectionError, RequestsError, TableError) as error:
        outcome = str(error)
        st.session_state.table_error = outcome
    else:
        st.session_state.records = records
        st.session_state.table_error = None
        status = "completed"
        outcome = f"Прочитано заявок — {len(records)}."
    finally:
        st.session_state.journal.append(JournalEntry.start(
            "Обновление таблицы", operation.events if operation is not None else (), status, outcome,
            api_key=settings.openai_api_key,
        ))


def _chat_outcome(reply: AgentReply) -> tuple[OperationStatus, str]:
    if reply.error:
        return "error", reply.text
    if reply.confirmation is not None:
        return "pending", f"Подготовлена смена статуса заявки №{reply.confirmation.request_id}."
    if reply.request_id is not None:
        return "completed", reply.text
    if reply.filter_status is not None and reply.records is not None:
        count = len(filter_records(reply.records, reply.filter_status))
        return "completed", f"Найдено заявок со статусом «{reply.filter_status}» — {count}."
    return "completed", "Ответ агента показан в чате."


def _handle_message(settings: Settings, message: str) -> None:
    state = st.session_state
    history = [(role, text) for role, text, error in state.messages if not error]
    state.messages.append(("user", message, False))
    with st.spinner("Выполняю запрос…"):
        reply = asyncio.run(run_turn(
            settings, message, allow_add=True, history=history, last_request_id=state.last_request_id
        ))
    journal_index = len(state.journal)
    status, outcome = _chat_outcome(reply)
    state.journal.append(JournalEntry.start(
        message, reply.events, status, outcome, api_key=settings.openai_api_key
    ))
    if reply.records is not None:
        state.records = reply.records
        state.table_error = None
    if reply.filter_status is not None and reply.records is not None and not reply.error:
        state.filter_status = reply.filter_status
    if reply.request_id is not None:
        state.last_request_id = reply.request_id
    if reply.confirmation is not None:
        state.pending_confirmation = PendingConfirmation.create(reply.confirmation, journal_index)
    state.messages.append(("assistant", reply.text, reply.error))


def _resolve_confirmation(settings: Settings, pending: PendingConfirmation, *, confirmed: bool) -> None:
    state = st.session_state
    current = state.pending_confirmation
    if current is None or current.operation_id != pending.operation_id:
        return
    if pending.operation_id in state.completed_operation_ids:
        return

    # Снимаем предложение до обращения к MCP, чтобы перезапуск не повторил запись.
    state.completed_operation_ids.add(pending.operation_id)
    state.pending_confirmation = None
    entry = state.journal[pending.journal_index]
    if not confirmed:
        text = f"Смена статуса заявки №{pending.change.request_id} отменена."
        state.journal[pending.journal_index] = entry.update(
            "cancelled", text, api_key=settings.openai_api_key
        )
        state.messages.append(("assistant", text, False))
        return

    with st.spinner("Проверяю и меняю статус через MCP…"):
        outcome = asyncio.run(confirm_status(settings, pending.change))
    status: OperationStatus = "error" if outcome.error else "completed"
    if outcome.replacement is not None:
        status = "pending"
    state.journal[pending.journal_index] = entry.update(
        status, outcome.text, api_key=settings.openai_api_key, events=outcome.events
    )
    if outcome.records is not None:
        state.records = outcome.records
        state.table_error = None
    if not outcome.error:
        state.last_request_id = pending.change.request_id
    state.messages.append(("assistant", outcome.text, outcome.error))
    if outcome.replacement is not None:
        state.pending_confirmation = PendingConfirmation.create(outcome.replacement, pending.journal_index)
        state.messages.append(("assistant", confirmation_text(outcome.replacement), False))


def _show_confirmation(settings: Settings) -> None:
    pending = st.session_state.pending_confirmation
    if pending is None:
        return
    confirm, cancel = st.columns(2)
    if confirm.button("Подтвердить", key=f"confirm_{pending.operation_id}", type="primary"):
        _resolve_confirmation(settings, pending, confirmed=True)
        st.rerun()
    if cancel.button("Отмена", key=f"cancel_{pending.operation_id}"):
        _resolve_confirmation(settings, pending, confirmed=False)
        st.rerun()


def _show_table() -> None:
    state = st.session_state
    if state.table_error:
        st.error(state.table_error)
        if state.records is not None:
            st.caption("Показаны данные последнего успешного чтения.")
    if state.records is None:
        return

    rows = filter_records(state.records, state.filter_status)
    if state.filter_status is not None:
        st.caption(f"Статус «{state.filter_status}» · {len(rows)} из {len(state.records)}")
    st.dataframe(
        {label: [row[index] for row in rows] for index, label in enumerate(HEADERS)},
        hide_index=True,
        width="stretch",
    )


def _show_journal(entries: list[JournalEntry]) -> None:
    with st.sidebar:
        st.subheader("Журнал MCP")
        for entry in reversed(entries):
            title = " ".join(entry.request.split())
            if len(title) > 48:
                title = f"{title[:47]}…"
            title = re.sub(r"([\\\x60*_{}\[\]()#+.!|<>&~:])", r"\\\1", title)
            with st.expander(f"{_OPERATION_LABELS[entry.status]} · {title}"):
                st.text(entry.request, width="stretch")
                st.caption("Итог")
                st.text(entry.outcome, width="stretch")
                if not entry.events:
                    st.caption("Рабочих MCP-вызовов нет.")
                for event in entry.events:
                    if isinstance(event, DeniedCall):
                        st.text(f"{event.name} · отклонён контроллером", width="stretch")
                        st.text(event.reason, width="stretch")
                    else:
                        duration = f"{event.duration_seconds * 1000:.0f} мс"
                        st.text(
                            f"{event.name} · {_CALL_LABELS[event.result.status]} · {duration}",
                            width="stretch",
                        )
                        st.json({
                            "Аргументы": event.arguments, "Ответ MCP": event.result.data,
                        }, expanded=False)


def _upgrade_journal(settings: Settings) -> None:
    state = st.session_state
    pending = state.pending_confirmation
    for index, entry in enumerate(state.journal):
        if isinstance(entry, tuple):
            request, events = entry
            waiting = pending is not None and pending.journal_index == index
            state.journal[index] = JournalEntry.start(
                request, events, "pending" if waiting else "recorded",
                "Ожидается подтверждение." if waiting else "Запись из предыдущей версии журнала.",
                api_key=settings.openai_api_key,
            )


def main() -> None:
    st.set_page_config(page_title="Excel AI Agent")
    st.title("Excel AI Agent")
    try:
        settings = load_settings()
    except SettingsError as error:
        st.error(str(error))
        return

    state = st.session_state
    source = (settings.excel_file_path, settings.excel_sheet_name)
    initial = state.get("source") != source
    defaults = {
        "records": None, "filter_status": None, "messages": [], "table_error": None, "journal": [],
        "last_request_id": None, "pending_confirmation": None, "completed_operation_ids": set(),
    }
    if initial:
        state.update(source=source, **defaults)
    else:
        for key, default in defaults.items():
            state.setdefault(key, default)
    _upgrade_journal(settings)

    st.subheader("Заявки")
    refresh, show_all = st.columns(2)
    reload_table = refresh.button("Обновить", key="refresh_table")
    if show_all.button("Показать все", key="show_all"):
        state.filter_status = None
    table = st.container()

    st.subheader("Чат")
    messages = st.container()
    confirmation = st.container()
    pending = state.pending_confirmation is not None
    message = (
        st.chat_input("Напишите запрос по заявкам", submit_mode="disable", disabled=pending) or ""
    ).strip()

    if initial or reload_table:
        with table, st.spinner("Читаю заявки через MCP…"):
            asyncio.run(_refresh_table(settings))
    if message and not pending:
        with messages:
            _handle_message(settings, message)
        if state.pending_confirmation is not None:
            st.rerun()

    with table:
        _show_table()
    with messages:
        for role, text, error in state.messages:
            with st.chat_message(role):
                if error:
                    st.error(text)
                else:
                    st.text(text, width="stretch")
    with confirmation:
        _show_confirmation(settings)
    _show_journal(state.journal)


main()
