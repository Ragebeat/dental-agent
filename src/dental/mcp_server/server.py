"""MCP-сервер (stdio): один процесс = один разговор = одна сессия DentalTools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from dental.config import load_config
from dental.core.service import ClinicCore
from dental.mcp_server.tools import DentalTools

INSTRUCTIONS = (
    "Запись пациентов в стоматологическую клинику. Личность подтверждается verify_identity (номер карты + дата "
    "рождения) или register_new_patient для нового пациента. Все времена по Токио. Свободные слоты берите только из "
    "search_slots, ничего не выдумывайте. При медицинских вопросах, жалобах, споре о правилах — handoff_to_staff."
)


def build_server(core: ClinicCore | None = None, *, now_fn=None, channel: str = "web", session_id: str | None = None) -> MCPServer:
    core = core or ClinicCore()
    now_fn = now_fn or load_config().now
    t = DentalTools(core, now_fn=now_fn, channel=channel, session_id=session_id)
    srv = MCPServer("dental-clinic", instructions=INSTRUCTIONS)

    @srv.tool()
    def list_services() -> dict:
        """Список услуг клиники: код, название, длительность, на сколько дней вперёд можно записаться, правила."""
        return t.list_services()

    @srv.tool()
    def search_slots(service: str, date_from: str, date_to: str, time_from: str | None = None,
                     time_to: str | None = None) -> dict:
        """Свободное время начала для услуги (code из list_services) за период. Даты YYYY-MM-DD, период не больше
        14 дней, time_from/time_to HH:MM — необязательный фильтр по времени суток. Список полный."""
        return t.search_slots(service, date_from, date_to, time_from, time_to)

    @srv.tool()
    def verify_identity(card_number: int, birth_date: str) -> dict:
        """Подтвердить личность пациента: номер карты и дата рождения (YYYY-MM-DD). После 5 неудач — блокировка."""
        return t.verify_identity(card_number, birth_date)

    @srv.tool()
    def register_new_patient(name: str, name_kana: str, phone: str, birth_date: str) -> dict:
        """Зарегистрировать нового пациента (имя, ふりがな хираганой, телефон, дата рождения YYYY-MM-DD); выдаёт номер карты."""
        return t.register_new_patient(name, name_kana, phone, birth_date)

    @srv.tool()
    def get_my_appointments() -> dict:
        """Предстоящие записи подтверждённого пациента."""
        return t.get_my_appointments()

    @srv.tool()
    def book_appointment(service: str, start: str, note: str | None = None) -> dict:
        """Записать подтверждённого пациента: service — code, start — YYYY-MM-DDTHH:MM (Токио), точно из search_slots."""
        return t.book_appointment(service, start, note)

    @srv.tool()
    def cancel_appointment(appointment_id: int) -> dict:
        """Отменить свою запись по appointment_id из get_my_appointments."""
        return t.cancel_appointment(appointment_id)

    @srv.tool()
    def reschedule_appointment(appointment_id: int, new_start: str) -> dict:
        """Перенести свою запись на новое время (YYYY-MM-DDTHH:MM, Токио). При отказе старая запись остаётся."""
        return t.reschedule_appointment(appointment_id, new_start)

    @srv.tool()
    def handoff_to_staff(reason: str, summary: str, appointment_id: int | None = None) -> dict:
        """Передать разговор сотруднику. reason: medical, ambiguity, late_cancel, manipulation, complaint,
        tool_errors, other. summary — краткая сводка по делу (до 1000 символов), без пересказа диалога."""
        return t.handoff_to_staff(reason, summary, appointment_id)

    srv._dental_tools = t   # для тестов и симулятора
    return srv


def main() -> None:
    build_server().run("stdio")
