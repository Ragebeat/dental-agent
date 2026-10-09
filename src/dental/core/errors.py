"""Типизированные ошибки ядра. В E2 каждая превращается в ответ тулзы: код + сообщение + подсказка."""

from __future__ import annotations

from datetime import date, datetime


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


class DentalError(Exception):
    code = "error"

    def __init__(self, message: str, **details) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **_plain(self.details)}


class InvalidInput(DentalError):            code = "invalid_input"
class ServiceUnavailable(DentalError):      code = "service_unavailable"
class ClosedDay(DentalError):               code = "closed_day"
class OutsideHours(DentalError):            code = "outside_hours"
class OutsideAllowedWindow(DentalError):    code = "outside_allowed_window"
class TooSoon(DentalError):                 code = "too_soon"
class BeyondHorizon(DentalError):           code = "beyond_horizon"
class SlotTaken(DentalError):               code = "slot_taken"          # details: alternatives
class LimitReached(DentalError):            code = "limit_reached"
class DuplicateBooking(DentalError):        code = "duplicate_booking"
class PatientBusy(DentalError):             code = "patient_busy"
class FirstVisitRequired(DentalError):      code = "first_visit_required"
class NewPatientOnly(DentalError):          code = "new_patient_only"
class IdempotencyConflict(DentalError):     code = "idempotency_conflict"
class TooLateToCancel(DentalError):         code = "too_late_to_cancel"  # → передача человеку
class NotFound(DentalError):                code = "not_found"
class AlreadyCancelled(DentalError):        code = "already_cancelled"
class PatientNotVerified(DentalError):      code = "patient_not_verified"
class Locked(DentalError):                  code = "locked"              # details: retry_after
class AlreadyRegistered(DentalError):       code = "already_registered"
