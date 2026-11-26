"""
authority_dashboard.py
Smart Campus Resource and Society Management Portal
Module owner : Neev Khandhedia (1025250192)
Responsibility: Authority Dashboard + polymorphism integration (view_dashboard)

WHAT THIS FILE DOES
-------------------
  * Approval queue   - authority sees pending room bookings, with warnings, and approves/rejects.
  * Room management  - add rooms, edit capacity/equipment, switch maintenance on/off.
  * Booking schedule - day-by-day, room-by-room view of approved bookings.
  * Dashboard view   - one dictionary with stats, queue, schedule, rooms and plug-in panels.
  * Polymorphism     - BaseDashboard + DashboardRegistry so every role's view_dashboard()
                       works through the same call.

WHAT THIS FILE DELIBERATELY DOES NOT DO  (other people's work - no overlap)
---------------------------------------------------------------------------
  * Creating bookings, time-slot validation, clash detection   -> Kamanishka
  * User / Student / Authority / SecurityGuard classes          -> Shivansh
  * Hostel night permissions and gate verification              -> Vatsal
  * Flask routes and HTML pages                                 -> built later, on top of this

This file does not import any other team member's file. It talks to them only through the
small "plug sockets" below, so everyone can finish in their own time.

INTEGRATION POINTS  (nothing in this file has to be edited for any of them)
---------------------------------------------------------------------------
  1. authority_check   -> pass  lambda u: isinstance(u, Authority)  using Shivansh's class.
  2. BookingGateway    -> Kamanishka writes a ~10-line adapter around her booking code.
  3. RoomGateway       -> whoever owns the Room class writes a ~6-line adapter.
  4. DashboardPanel    -> anyone adds a card to the dashboard, e.g. Vatsal's night permissions:

         class NightPermissionPanel(DashboardPanel):
             key, title = "night_permissions", "Hostel night permissions"
             def __init__(self, night_service): self._svc = night_service
             def summary(self, authority):
                 return {"pending": len(self._svc.pending_requests(authority))}

         service.register_panel(NightPermissionPanel(night_service))

  5. service.events    -> subscribe to booking_approved / booking_rejected / room_* events
                          (e.g. for notifications) without touching this file.
  6. DashboardRegistry -> Shivansh's User.view_dashboard() just calls  registry.render_for(self).
  7. ist_now()         -> identical 3-line helper also exists in night_permissions.py; when the
                          team creates a shared utils.py, delete it here and import it instead.

All times are Indian Standard Time (IST) as plain datetimes, same as the rest of the project.
"""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

logger = logging.getLogger("authority_dashboard")


# =====================================================================================
# 0. INDIAN STANDARD TIME
# =====================================================================================
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def ist_now() -> datetime:
    """Current Indian time, whatever timezone the server/PC is set to."""
    return datetime.now(IST).replace(tzinfo=None)


# =====================================================================================
# 1. EXCEPTIONS  (all start with "Dashboard" so they never clash with other modules)
# =====================================================================================
class DashboardError(Exception):
    """Base class for every error raised by this module."""


class DashboardValidationError(DashboardError):
    """Bad input or a rule violation (empty remarks, past slot, room under maintenance...)."""


class DashboardAccessError(DashboardError):
    """The user is not allowed to do this (not an authority)."""


class DashboardNotFoundError(DashboardError):
    """Booking, room or dashboard type does not exist."""


# =====================================================================================
# 2. DATA SHAPES  (small read-only objects used to talk to other modules)
# =====================================================================================
class BookingStatus(Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RoomStatus(Enum):
    AVAILABLE = "AVAILABLE"
    MAINTENANCE = "MAINTENANCE"


class DecisionType(Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class BookingInfo:
    """What the dashboard needs to know about a booking. The booking module fills this in."""
    booking_id: int
    society_name: str
    room_id: int
    start: datetime
    end: datetime
    purpose: str
    status: BookingStatus = BookingStatus.PENDING
    requested_by: str = ""
    expected_attendees: Optional[int] = None
    requested_at: Optional[datetime] = None
    notes: str = ""          # free text from the booking module, e.g. "possible clash with #9"


@dataclass(frozen=True)
class RoomInfo:
    """A room as the dashboard sees it. room_id is None until the room is saved."""
    room_id: Optional[int]
    name: str
    capacity: int
    equipment: Tuple[str, ...] = ()
    status: RoomStatus = RoomStatus.AVAILABLE
    maintenance_note: str = ""


@dataclass(frozen=True)
class Decision:
    """One line of the authority's audit trail."""
    decision_id: int
    booking_id: int
    authority_id: str
    authority_name: str
    decision: DecisionType
    remarks: str
    decided_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {"decision_id": self.decision_id, "booking_id": self.booking_id,
                "authority_id": self.authority_id, "authority": self.authority_name,
                "decision": self.decision.value, "remarks": self.remarks,
                "decided_at": self.decided_at}


class UserLike(Protocol):
    """Anything with these two attributes works - Shivansh's User classes will."""
    user_id: str
    name: str


# =====================================================================================
# 3. PLUG SOCKETS FOR OTHER MODULES  (Protocols = "your class just needs these methods")
# =====================================================================================
class BookingGateway(Protocol):
    """
    Socket for Kamanishka's booking module.
    approve_booking() is where HER clash detection must run; if it finds a clash it raises
    its own exception, which passes through the dashboard untouched (nothing is logged).
    bookings_between() returns bookings of ANY status that overlap [start, end).
    """
    def pending_bookings(self) -> List[BookingInfo]: ...
    def get_booking(self, booking_id: int) -> Optional[BookingInfo]: ...
    def approve_booking(self, booking_id: int, authority_id: str, now: datetime) -> None: ...
    def reject_booking(self, booking_id: int, authority_id: str, remarks: str,
                       now: datetime) -> None: ...
    def bookings_between(self, start: datetime, end: datetime,
                         room_id: Optional[int] = None) -> List[BookingInfo]: ...


class RoomGateway(Protocol):
    """
    Socket for whoever owns the Room class / room table.
    save_room(): room_id None means insert (return the room WITH its new id); otherwise update.
    """
    def list_rooms(self) -> List[RoomInfo]: ...
    def get_room(self, room_id: int) -> Optional[RoomInfo]: ...
    def save_room(self, room: RoomInfo) -> RoomInfo: ...


# =====================================================================================
# 4. EXTENSION POINTS  (events + dashboard panels)
# =====================================================================================
class DashboardEvent(Enum):
    BOOKING_APPROVED = "booking_approved"
    BOOKING_REJECTED = "booking_rejected"
    ROOM_ADDED = "room_added"
    ROOM_UPDATED = "room_updated"
    ROOM_MAINTENANCE_CHANGED = "room_maintenance_changed"


class EventBus:
    """Other modules subscribe; the dashboard publishes. A faulty subscriber never breaks it."""

    def __init__(self) -> None:
        self._subscribers: Dict[DashboardEvent, List[Callable[[Dict[str, Any]], None]]] = {}

    def subscribe(self, event: DashboardEvent, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._subscribers.setdefault(event, []).append(callback)

    def publish(self, event: DashboardEvent, payload: Dict[str, Any]) -> None:
        for callback in list(self._subscribers.get(event, [])):
            try:
                callback(payload)
            except Exception:                      # isolate other people's bugs
                logger.exception("Subscriber failed for event %s", event.value)


class DashboardPanel(ABC):
    """
    A card other modules can add to the authority dashboard.
    Subclass it, set `key` and `title`, implement summary(), then service.register_panel(...).
    """
    key: str = ""
    title: str = ""

    @abstractmethod
    def summary(self, authority: UserLike) -> Dict[str, Any]:
        """Return a small dict of numbers/lists for this card."""


# =====================================================================================
# 5. DECISION LOG  (the dashboard's own SQLite table: dashboard_decisions)
# =====================================================================================
class DecisionLog:
    """Audit trail of who approved/rejected what, and when. Table name is unique to this module."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS dashboard_decisions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER NOT NULL, "
            "authority_id TEXT NOT NULL, authority_name TEXT NOT NULL, decision TEXT NOT NULL, "
            "remarks TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL)")
        self._conn.commit()

    @staticmethod
    def _to_decision(row: sqlite3.Row) -> Decision:
        return Decision(row["id"], row["booking_id"], row["authority_id"], row["authority_name"],
                        DecisionType(row["decision"]), row["remarks"],
                        datetime.fromisoformat(row["decided_at"]))

    def record(self, booking_id: int, authority_id: str, authority_name: str,
               decision: DecisionType, remarks: str, when: datetime) -> Decision:
        cur = self._conn.execute(
            "INSERT INTO dashboard_decisions (booking_id, authority_id, authority_name, "
            "decision, remarks, decided_at) VALUES (?, ?, ?, ?, ?, ?)",
            (booking_id, authority_id, authority_name, decision.value, remarks, when.isoformat()))
        self._conn.commit()
        new_id = cur.lastrowid
        if new_id is None:
            raise DashboardError("Database did not return an id for the decision.")
        return Decision(new_id, booking_id, authority_id, authority_name, decision, remarks, when)

    def recent(self, limit: int = 20) -> List[Decision]:
        rows = self._conn.execute(
            "SELECT * FROM dashboard_decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._to_decision(r) for r in rows]

    def count_on(self, day: date, decision: DecisionType) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM dashboard_decisions WHERE decision = ? "
            "AND substr(decided_at, 1, 10) = ?", (decision.value, day.isoformat())).fetchone()
        return int(row[0])


# =====================================================================================
# 6. AUTHORITY SERVICE  (all authority actions and queries; Flask routes will call this)
# =====================================================================================
class AuthorityService:
    UPCOMING_DAYS = 7          # horizon for "upcoming bookings" and the maintenance check
    MAX_SCHEDULE_DAYS = 31     # longest range the schedule view will return

    def __init__(self, bookings: BookingGateway, rooms: RoomGateway,
                 authority_check: Callable[[object], bool],
                 db_path: str = "campus.db",
                 clock: Callable[[], datetime] = ist_now):
        self._bookings = bookings
        self._rooms = rooms
        self._is_authority = authority_check
        self._log = DecisionLog(db_path)
        self._clock = clock
        self._panels: Dict[str, DashboardPanel] = {}
        self.events = EventBus()

    def now(self) -> datetime:
        return self._clock()

    def _require(self, user: UserLike, action: str) -> None:
        if not self._is_authority(user):
            raise DashboardAccessError(f"Only an authority can {action}.")

    # ---------- plug-in panels ----------
    def register_panel(self, panel: DashboardPanel) -> None:
        if not panel.key.strip() or not panel.title.strip():
            raise DashboardValidationError("A panel needs both a key and a title.")
        if panel.key in self._panels:
            raise DashboardValidationError(f"A panel with key '{panel.key}' is already registered.")
        self._panels[panel.key] = panel

    def panel_keys(self) -> List[str]:
        return list(self._panels)

    def _panel_summaries(self, authority: UserLike) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for key, panel in self._panels.items():
            try:
                result[key] = {"title": panel.title, "data": panel.summary(authority), "error": None}
            except Exception as exc:               # one broken panel must not kill the dashboard
                logger.exception("Panel '%s' failed", key)
                result[key] = {"title": panel.title, "data": None,
                               "error": f"{type(exc).__name__}: {exc}"}
        return result

    # ---------- helpers ----------
    def _room_map(self) -> Dict[int, RoomInfo]:
        return {r.room_id: r for r in self._rooms.list_rooms() if r.room_id is not None}

    @staticmethod
    def _room_dict(room: RoomInfo, upcoming: int = 0) -> Dict[str, Any]:
        return {"room_id": room.room_id, "name": room.name, "capacity": room.capacity,
                "equipment": list(room.equipment), "status": room.status.value,
                "maintenance_note": room.maintenance_note, "upcoming_bookings": upcoming}

    def _flags(self, b: BookingInfo, room: Optional[RoomInfo]) -> List[str]:
        """Warnings shown next to a pending booking. Informational, not clash detection."""
        flags: List[str] = []
        if room is None:
            flags.append("Room not found in the room list.")
        else:
            if room.status is RoomStatus.MAINTENANCE:
                flags.append(f"{room.name} is under maintenance.")
            if b.expected_attendees is not None and b.expected_attendees > room.capacity:
                flags.append(f"Expected {b.expected_attendees} attendees but "
                             f"{room.name} holds {room.capacity}.")
        if b.end <= self.now():
            flags.append("This time slot has already passed.")
        if b.notes:
            flags.append(f"Booking module: {b.notes}")
        return flags

    def _booking_dict(self, b: BookingInfo, rooms: Dict[int, RoomInfo],
                      with_flags: bool = False) -> Dict[str, Any]:
        room = rooms.get(b.room_id)
        item: Dict[str, Any] = {
            "booking_id": b.booking_id, "society": b.society_name, "room_id": b.room_id,
            "room": room.name if room else f"Room #{b.room_id}", "start": b.start, "end": b.end,
            "purpose": b.purpose, "status": b.status.value, "requested_by": b.requested_by,
            "expected_attendees": b.expected_attendees, "requested_at": b.requested_at,
            "notes": b.notes}
        if with_flags:
            item["flags"] = self._flags(b, room)
        return item

    def _pending_booking(self, booking_id: int) -> BookingInfo:
        booking = self._bookings.get_booking(booking_id)
        if booking is None:
            raise DashboardNotFoundError(f"Booking #{booking_id} not found.")
        if booking.status is not BookingStatus.PENDING:
            raise DashboardValidationError(
                f"Booking #{booking_id} is already {booking.status.value}.")
        return booking

    # ---------- approval queue ----------
    def pending_queue(self, authority: UserLike) -> List[Dict[str, Any]]:
        self._require(authority, "view the approval queue")
        rooms = self._room_map()
        pending = sorted(self._bookings.pending_bookings(), key=lambda b: (b.start, b.booking_id))
        return [self._booking_dict(b, rooms, with_flags=True) for b in pending]

    def approve(self, authority: UserLike, booking_id: int) -> BookingInfo:
        self._require(authority, "approve bookings")
        booking = self._pending_booking(booking_id)
        now = self.now()
        if booking.end <= now:
            raise DashboardValidationError("This booking's time slot has already passed.")
        room = self._rooms.get_room(booking.room_id)
        if room is None:
            raise DashboardValidationError(f"Room #{booking.room_id} no longer exists.")
        if room.status is RoomStatus.MAINTENANCE:
            raise DashboardValidationError(
                f"{room.name} is under maintenance. Reject this booking or end the maintenance first.")

        self._bookings.approve_booking(booking_id, authority.user_id, now)   # clash check lives here
        self._log.record(booking_id, authority.user_id, authority.name,
                         DecisionType.APPROVED, "", now)
        self.events.publish(DashboardEvent.BOOKING_APPROVED,
                            {"booking_id": booking_id, "authority_id": authority.user_id,
                             "society": booking.society_name, "room_id": booking.room_id,
                             "start": booking.start, "end": booking.end, "at": now})
        return self._bookings.get_booking(booking_id) or booking

    def reject(self, authority: UserLike, booking_id: int, remarks: str) -> BookingInfo:
        self._require(authority, "reject bookings")
        remarks = (remarks or "").strip()
        if not remarks:
            raise DashboardValidationError("Please give a reason for rejecting - the society will see it.")
        booking = self._pending_booking(booking_id)
        now = self.now()

        self._bookings.reject_booking(booking_id, authority.user_id, remarks, now)
        self._log.record(booking_id, authority.user_id, authority.name,
                         DecisionType.REJECTED, remarks, now)
        self.events.publish(DashboardEvent.BOOKING_REJECTED,
                            {"booking_id": booking_id, "authority_id": authority.user_id,
                             "society": booking.society_name, "room_id": booking.room_id,
                             "remarks": remarks, "at": now})
        return self._bookings.get_booking(booking_id) or booking

    def decision_history(self, authority: UserLike, limit: int = 20) -> List[Decision]:
        self._require(authority, "view the decision history")
        return self._log.recent(max(1, min(limit, 200)))

    # ---------- room management ----------
    @staticmethod
    def _clean_name(name: str) -> str:
        name = (name or "").strip()
        if not 2 <= len(name) <= 60:
            raise DashboardValidationError("Room name must be 2-60 characters.")
        return name

    @staticmethod
    def _clean_capacity(capacity: int) -> int:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 5000:
            raise DashboardValidationError("Capacity must be a whole number from 1 to 5000.")
        return capacity

    @staticmethod
    def _clean_equipment(items: Optional[Iterable[str]]) -> Tuple[str, ...]:
        cleaned: List[str] = []
        for item in items or ():
            text = str(item).strip()
            if text and text.lower() not in (c.lower() for c in cleaned):
                cleaned.append(text)
        return tuple(cleaned)

    def _check_name_free(self, name: str, ignore_room_id: Optional[int] = None) -> None:
        for room in self._rooms.list_rooms():
            if room.room_id != ignore_room_id and room.name.lower() == name.lower():
                raise DashboardValidationError(f"A room named '{room.name}' already exists.")

    def _upcoming_approved(self, room_id: Optional[int] = None) -> List[BookingInfo]:
        now = self.now()
        window = self._bookings.bookings_between(
            now, now + timedelta(days=self.UPCOMING_DAYS), room_id)
        return [b for b in window if b.status is BookingStatus.APPROVED]

    def rooms_overview(self, authority: UserLike) -> List[Dict[str, Any]]:
        self._require(authority, "view rooms")
        counts: Dict[int, int] = {}
        for b in self._upcoming_approved():
            counts[b.room_id] = counts.get(b.room_id, 0) + 1
        rooms = sorted(self._rooms.list_rooms(), key=lambda r: r.name.lower())
        return [self._room_dict(r, counts.get(r.room_id, 0) if r.room_id is not None else 0)
                for r in rooms]

    def add_room(self, authority: UserLike, name: str, capacity: int,
                 equipment: Optional[Iterable[str]] = None) -> RoomInfo:
        self._require(authority, "add rooms")
        name, capacity = self._clean_name(name), self._clean_capacity(capacity)
        self._check_name_free(name)
        saved = self._rooms.save_room(RoomInfo(None, name, capacity, self._clean_equipment(equipment)))
        self.events.publish(DashboardEvent.ROOM_ADDED,
                            {"room_id": saved.room_id, "name": saved.name, "by": authority.user_id})
        return saved

    def update_room(self, authority: UserLike, room_id: int, name: Optional[str] = None,
                    capacity: Optional[int] = None,
                    equipment: Optional[Iterable[str]] = None) -> RoomInfo:
        self._require(authority, "edit rooms")
        room = self._rooms.get_room(room_id)
        if room is None:
            raise DashboardNotFoundError(f"Room #{room_id} not found.")
        if name is not None:
            name = self._clean_name(name)
            self._check_name_free(name, ignore_room_id=room_id)
            room = replace(room, name=name)
        if capacity is not None:
            room = replace(room, capacity=self._clean_capacity(capacity))
        if equipment is not None:
            room = replace(room, equipment=self._clean_equipment(equipment))
        saved = self._rooms.save_room(room)
        self.events.publish(DashboardEvent.ROOM_UPDATED,
                            {"room_id": saved.room_id, "name": saved.name, "by": authority.user_id})
        return saved

    def set_maintenance(self, authority: UserLike, room_id: int, under_maintenance: bool,
                        note: str = "", force: bool = False) -> RoomInfo:
        """
        Switch maintenance on/off. Switching ON is refused while approved bookings are coming
        up in that room, unless force=True. The event payload lists the affected booking ids
        so the booking module can react (e.g. re-suggest rooms).
        """
        self._require(authority, "change maintenance status")
        room = self._rooms.get_room(room_id)
        if room is None:
            raise DashboardNotFoundError(f"Room #{room_id} not found.")

        affected: List[int] = []
        if under_maintenance:
            note = (note or "").strip()
            if not note:
                raise DashboardValidationError("Please describe the maintenance work.")
            affected = [b.booking_id for b in self._upcoming_approved(room_id)]
            if affected and not force:
                raise DashboardValidationError(
                    f"{len(affected)} approved booking(s) in the next {self.UPCOMING_DAYS} days use "
                    f"{room.name}. Reschedule them first, or confirm with force=True.")
            room = replace(room, status=RoomStatus.MAINTENANCE, maintenance_note=note)
        else:
            room = replace(room, status=RoomStatus.AVAILABLE, maintenance_note="")

        saved = self._rooms.save_room(room)
        self.events.publish(DashboardEvent.ROOM_MAINTENANCE_CHANGED,
                            {"room_id": saved.room_id, "name": saved.name,
                             "under_maintenance": under_maintenance, "note": saved.maintenance_note,
                             "affected_booking_ids": affected, "by": authority.user_id})
        return saved

    # ---------- booking schedule ----------
    def schedule(self, authority: UserLike, start: date, end: date, room_id: Optional[int] = None,
                 include_pending: bool = False) -> Dict[str, List[Dict[str, Any]]]:
        """Returns {'2026-11-22': [booking dicts...], ...}; every day in range is a key."""
        self._require(authority, "view the booking schedule")
        if end < start:
            raise DashboardValidationError("End date must not be before the start date.")
        if (end - start).days >= self.MAX_SCHEDULE_DAYS:
            raise DashboardValidationError(f"Please pick at most {self.MAX_SCHEDULE_DAYS} days.")

        wanted = [BookingStatus.APPROVED]
        if include_pending:
            wanted.append(BookingStatus.PENDING)

        days: Dict[str, List[Dict[str, Any]]] = {}
        d = start
        while d <= end:
            days[d.isoformat()] = []
            d += timedelta(days=1)

        rooms = self._room_map()
        found = self._bookings.bookings_between(
            datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min),
            room_id)
        for b in sorted(found, key=lambda x: (x.start, x.booking_id)):
            if b.status not in wanted:
                continue
            key = max(b.start.date(), start).isoformat()   # multi-day bookings sit on first visible day
            if key in days:
                days[key].append(self._booking_dict(b, rooms))
        return days

    def todays_schedule(self, authority: UserLike, room_id: Optional[int] = None) -> List[Dict[str, Any]]:
        today = self.now().date()
        return self.schedule(authority, today, today, room_id)[today.isoformat()]

    # ---------- the full dashboard ----------
    def dashboard_data(self, authority: UserLike) -> Dict[str, Any]:
        self._require(authority, "open the dashboard")
        now = self.now()
        queue = self.pending_queue(authority)
        rooms = self.rooms_overview(authority)
        today_list = self.todays_schedule(authority)
        return {
            "authority": {"id": authority.user_id, "name": authority.name},
            "generated_at": now,
            "stats": {
                "pending_bookings": len(queue),
                "approved_today": self._log.count_on(now.date(), DecisionType.APPROVED),
                "rejected_today": self._log.count_on(now.date(), DecisionType.REJECTED),
                "bookings_today": len(today_list),
                "rooms_total": len(rooms),
                "rooms_in_maintenance": sum(
                    1 for r in rooms if r["status"] == RoomStatus.MAINTENANCE.value),
            },
            "pending_queue": queue,
            "todays_schedule": today_list,
            "rooms": rooms,
            "recent_decisions": [d.to_dict() for d in self._log.recent(5)],
            "panels": self._panel_summaries(authority),
        }


# =====================================================================================
# 7. POLYMORPHIC DASHBOARDS  (view_dashboard for every role goes through one door)
# =====================================================================================
class BaseDashboard(ABC):
    """Every role's dashboard inherits this and implements render() its own way."""
    title: str = "Dashboard"

    def __init__(self, user: UserLike):
        self._user = user

    @property
    def user(self) -> UserLike:
        return self._user

    @abstractmethod
    def render(self) -> Dict[str, Any]:
        """Return a view-model dict that a template (or console) can display."""


class AuthorityDashboard(BaseDashboard):
    title = "Authority Dashboard"

    def __init__(self, user: UserLike, service: AuthorityService):
        super().__init__(user)
        self._service = service

    def render(self) -> Dict[str, Any]:
        data = self._service.dashboard_data(self._user)
        data["title"] = self.title
        return data


class DashboardRegistry:
    """
    Maps user types to dashboards, so  user.view_dashboard()  can simply be:
        return registry.render_for(self)
    Register more specific classes first (the first matching isinstance wins).
    """

    def __init__(self) -> None:
        self._factories: List[Tuple[type, Callable[[Any], BaseDashboard]]] = []

    def register(self, user_type: type, factory: Callable[[Any], BaseDashboard]) -> None:
        self._factories.append((user_type, factory))

    def dashboard_for(self, user: object) -> BaseDashboard:
        for user_type, factory in self._factories:
            if isinstance(user, user_type):
                return factory(user)
        raise DashboardNotFoundError(f"No dashboard registered for {type(user).__name__}.")

    def render_for(self, user: object) -> Dict[str, Any]:
        return self.dashboard_for(user).render()


# =====================================================================================
# 8. STAND-INS  (so this file runs alone - DELETE this section once real modules exist)
# =====================================================================================
class InMemoryBookingGateway:
    """Stands in for Kamanishka's booking module. Real approve_booking() runs clash detection."""

    def __init__(self) -> None:
        self._items: Dict[int, BookingInfo] = {}
        self._next_id = 1

    def add(self, society_name: str, room_id: int, start: datetime, end: datetime, purpose: str,
            expected_attendees: Optional[int] = None, notes: str = "",
            status: BookingStatus = BookingStatus.PENDING) -> BookingInfo:
        info = BookingInfo(self._next_id, society_name, room_id, start, end, purpose, status,
                           expected_attendees=expected_attendees, notes=notes)
        self._items[info.booking_id] = info
        self._next_id += 1
        return info

    def pending_bookings(self) -> List[BookingInfo]:
        return [b for b in self._items.values() if b.status is BookingStatus.PENDING]

    def get_booking(self, booking_id: int) -> Optional[BookingInfo]:
        return self._items.get(booking_id)

    def approve_booking(self, booking_id: int, authority_id: str, now: datetime) -> None:
        self._items[booking_id] = replace(self._items[booking_id], status=BookingStatus.APPROVED)

    def reject_booking(self, booking_id: int, authority_id: str, remarks: str, now: datetime) -> None:
        self._items[booking_id] = replace(self._items[booking_id], status=BookingStatus.REJECTED)

    def bookings_between(self, start: datetime, end: datetime,
                         room_id: Optional[int] = None) -> List[BookingInfo]:
        return [b for b in self._items.values()
                if b.start < end and start < b.end and (room_id is None or b.room_id == room_id)]


class InMemoryRoomGateway:
    """Stands in for the real Room storage."""

    def __init__(self) -> None:
        self._rooms: Dict[int, RoomInfo] = {}
        self._next_id = 1

    def list_rooms(self) -> List[RoomInfo]:
        return list(self._rooms.values())

    def get_room(self, room_id: int) -> Optional[RoomInfo]:
        return self._rooms.get(room_id)

    def save_room(self, room: RoomInfo) -> RoomInfo:
        if room.room_id is None:
            room = replace(room, room_id=self._next_id)
            self._next_id += 1
        assert room.room_id is not None
        self._rooms[room.room_id] = room
        return room


class _StubUser:
    def __init__(self, user_id: str, name: str):
        self.user_id, self.name = user_id, name


class _StubAuthority(_StubUser):
    """Placeholder for Shivansh's Authority class."""


class _StubStudent(_StubUser):
    """Placeholder for Shivansh's Student class."""


# =====================================================================================
# 9. DEMO  -  run:  python authority_dashboard.py
# =====================================================================================
class _DemoClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


class _DemoPanel(DashboardPanel):
    """Shows how another module adds a card (stands in for e.g. night permissions)."""
    key, title = "demo_panel", "Example plug-in panel"

    def summary(self, authority: UserLike) -> Dict[str, Any]:
        return {"pending_items": 3}


class _BrokenPanel(DashboardPanel):
    key, title = "broken_panel", "Panel that crashes"

    def summary(self, authority: UserLike) -> Dict[str, Any]:
        raise RuntimeError("something went wrong inside this panel")


class _DemoStudentDashboard(BaseDashboard):
    title = "Student Dashboard"

    def render(self) -> Dict[str, Any]:
        return {"title": self.title, "message": f"Hello {self._user.name}, no bookings yet."}


def _print_dashboard(data: Dict[str, Any]) -> None:
    s = data["stats"]
    print(f"  == {data['title']} - {data['authority']['name']} - {data['generated_at']:%d %b %I:%M %p} ==")
    print(f"  Pending {s['pending_bookings']} | approved today {s['approved_today']} | "
          f"rejected today {s['rejected_today']} | bookings today {s['bookings_today']} | "
          f"rooms {s['rooms_total']} ({s['rooms_in_maintenance']} in maintenance)")
    for q in data["pending_queue"]:
        print(f"  QUEUE  #{q['booking_id']} {q['society']} - {q['room']} "
              f"{q['start']:%d %b %I:%M %p}-{q['end']:%I:%M %p} ({q['purpose']})")
        for flag in q["flags"]:
            print(f"           ! {flag}")
    for t in data["todays_schedule"]:
        print(f"  TODAY  {t['start']:%I:%M %p}-{t['end']:%I:%M %p} {t['room']}: {t['society']}")
    for r in data["rooms"]:
        print(f"  ROOM   {r['name']} (cap {r['capacity']}, {r['status']}, "
              f"{r['upcoming_bookings']} upcoming) {', '.join(r['equipment'])}")
    for key, p in data["panels"].items():
        print(f"  PANEL  {p['title']}: {p['data'] if p['error'] is None else 'ERROR - ' + p['error']}")


def _demo() -> None:
    clock = _DemoClock(datetime(2026, 11, 22, 10, 0))
    bookings, rooms = InMemoryBookingGateway(), InMemoryRoomGateway()
    service = AuthorityService(bookings, rooms,
                               authority_check=lambda u: isinstance(u, _StubAuthority),
                               db_path=":memory:", clock=clock)
    authority, student = _StubAuthority("AUTH01", "Dean Kaur"), _StubStudent("1025250199", "Demo Student")

    hall = rooms.save_room(RoomInfo(None, "Seminar Hall", 120, ("Projector", "Mic")))
    r204 = rooms.save_room(RoomInfo(None, "Room 204", 40, ("Whiteboard",)))
    audi = rooms.save_room(RoomInfo(None, "Auditorium", 500, ("Stage", "Sound system")))
    assert hall.room_id and r204.room_id and audi.room_id
    d = lambda day, hh: datetime(2026, 11, day, hh, 0)                   # noqa: E731
    bookings.add("Drama Society", hall.room_id, d(22, 16), d(22, 19), "Rehearsal", 60)
    bookings.add("Coding Club", r204.room_id, d(23, 14), d(23, 16), "Workshop", 55)
    bookings.add("Music Society", audi.room_id, d(22, 18), d(22, 21), "Concert", 300,
                 status=BookingStatus.APPROVED)
    bookings.add("Robotics Club", r204.room_id, d(25, 11), d(25, 13), "Demo day", 30,
                 notes="possible overlap with another request")

    service.register_panel(_DemoPanel())
    service.register_panel(_BrokenPanel())
    service.events.subscribe(DashboardEvent.BOOKING_APPROVED,
                             lambda p: print(f"     [event] booking #{p['booking_id']} approved "
                                             f"-> notify {p['society']}"))
    service.events.subscribe(DashboardEvent.ROOM_MAINTENANCE_CHANGED,
                             lambda p: print(f"     [event] {p['name']} maintenance="
                                             f"{p['under_maintenance']}, affected {p['affected_booking_ids']}"))

    def attempt(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            print(f"   OK      {label}")
        except DashboardError as e:
            print(f"   BLOCKED {label} -> {e}")

    print("1) Dashboard as the authority sees it:")
    registry = DashboardRegistry()
    registry.register(_StubAuthority, lambda u: AuthorityDashboard(u, service))
    registry.register(_StubStudent, _DemoStudentDashboard)
    _print_dashboard(registry.render_for(authority))

    print("\n2) Approval workflow:")
    attempt("approve #1", lambda: service.approve(authority, 1))
    attempt("approve #1 again", lambda: service.approve(authority, 1))
    attempt("reject #2 with no reason", lambda: service.reject(authority, 2, "  "))
    attempt("reject #2 (room too small)", lambda: service.reject(authority, 2, "Room too small for 55 people"))

    print("\n3) Room management:")
    attempt("add 'Lab 3' capacity 30", lambda: service.add_room(authority, "Lab 3", 30, ["PCs", "pcs", "AC"]))
    attempt("add duplicate 'seminar hall'", lambda: service.add_room(authority, "seminar hall", 50))
    attempt("add room capacity -5", lambda: service.add_room(authority, "Ghost Room", -5))
    attempt("maintenance on Auditorium (booked)", lambda: service.set_maintenance(
        authority, audi.room_id or 0, True, "Stage repair"))
    attempt("maintenance on Auditorium (forced)", lambda: service.set_maintenance(
        authority, audi.room_id or 0, True, "Stage repair", force=True))
    attempt("maintenance on Room 204", lambda: service.set_maintenance(
        authority, r204.room_id or 0, True, "Projector wiring"))
    attempt("approve #4 in room under maintenance", lambda: service.approve(authority, 4))

    print("\n4) Schedule 22-24 Nov (approved only):")
    for day, items in service.schedule(authority, date(2026, 11, 22), date(2026, 11, 24)).items():
        print(f"   {day}: " + (", ".join(f"{i['room']} {i['start']:%I:%M %p} {i['society']}"
                                         for i in items) or "-"))

    print("\n5) Access control and polymorphism:")
    attempt("student opens authority dashboard", lambda: AuthorityDashboard(student, service).render())
    print("   Student via registry ->", registry.render_for(student)["message"])

    print("\n6) Final dashboard:")
    _print_dashboard(registry.render_for(authority))


if __name__ == "__main__":
    _demo()