"""
night_permissions.py
Smart Campus Resource and Society Management Portal
Module owner : Vatsal Sharma (1025250198)
Responsibility: Encapsulation, Hostel Night Permissions, Security Verification

How the flow works
------------------
1. Student submits a request to leave the hostel after curfew (10:00 PM).
2. Authority approves / rejects it. On approval a 6-digit verification code is issued.
3. At the gate, the SecurityGuard verifies the student with the code  -> status OUT.
4. When the student comes back, the guard records the return        -> status RETURNED
   (flagged "late" if beyond the expected return time + grace period).

Time zone
---------
Every timestamp is Indian Standard Time (IST, UTC+05:30), taken from ist_now(),
so curfew checks and gate records are correct even if the server runs on UTC.

Integration note
----------------
The User / Student / Authority / SecurityGuard classes in the "USER STUBS" section are
minimal placeholders so this file runs on its own. Once Shivansh's real user classes are
ready, delete the stubs and import his classes instead - the service only relies on
`user_id`, `name` and `isinstance(...)` checks.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional


# =====================================================================================
# 0. INDIAN STANDARD TIME
# =====================================================================================
# India has no daylight saving, so a fixed UTC+05:30 offset is always correct (and needs
# no extra packages on Windows). All times in this module are IST wall-clock times,
# stored as plain (naive) datetimes so they match what <input type="datetime-local"> sends.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def ist_now() -> datetime:
    """Current Indian time, no matter which timezone the server/PC is set to."""
    return datetime.now(IST).replace(tzinfo=None)


# =====================================================================================
# 1. POLICY  (change the curfew here - nothing else in the code needs to be touched)
# =====================================================================================
@dataclass(frozen=True)
class NightPolicy:
    curfew_time: time = time(22, 0)        # 10:00 PM - current campus rule
    night_end_time: time = time(6, 0)      # hostel "night" ends at 6:00 AM
    max_advance_days: int = 7              # how far ahead a request can be made
    max_duration_hours: int = 8            # max time out of hostel per permission
    return_grace_minutes: int = 15         # grace before a return is marked late
    early_exit_minutes: int = 30           # guard can verify this early before planned exit
    min_reason_length: int = 5

    def is_night_time(self, t: time) -> bool:
        """True if a clock time falls in the restricted night window."""
        return t >= self.curfew_time or t < self.night_end_time

    @property
    def curfew_label(self) -> str:
        return self.curfew_time.strftime("%I:%M %p").lstrip("0")


# =====================================================================================
# 2. EXCEPTIONS
# =====================================================================================
class NightPermissionError(Exception):
    """Base class for all night-permission errors."""


class ValidationError(NightPermissionError):
    """Bad input or a rule violation."""


class InvalidTransitionError(NightPermissionError):
    """Status change that is not allowed (e.g. approving a rejected request)."""


class AuthorizationError(NightPermissionError):
    """Wrong role, wrong owner, or wrong verification code."""


class NotFoundError(NightPermissionError):
    """Permission id does not exist."""


# =====================================================================================
# 3. STATUS + ALLOWED TRANSITIONS
# =====================================================================================
class PermissionStatus(Enum):
    PENDING = "PENDING"        # waiting for authority
    APPROVED = "APPROVED"      # approved, student has not left yet
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"    # withdrawn by student
    EXPIRED = "EXPIRED"        # approved but never used before return time
    OUT = "OUT"                # guard verified exit, student is outside
    RETURNED = "RETURNED"      # guard recorded return


_ALLOWED: Dict[PermissionStatus, set] = {
    PermissionStatus.PENDING:   {PermissionStatus.APPROVED, PermissionStatus.REJECTED,
                                 PermissionStatus.CANCELLED},
    PermissionStatus.APPROVED:  {PermissionStatus.OUT, PermissionStatus.CANCELLED,
                                 PermissionStatus.EXPIRED},
    PermissionStatus.OUT:       {PermissionStatus.RETURNED},
    PermissionStatus.REJECTED:  set(),
    PermissionStatus.CANCELLED: set(),
    PermissionStatus.EXPIRED:   set(),
    PermissionStatus.RETURNED:  set(),
}
_ACTIVE = (PermissionStatus.PENDING, PermissionStatus.APPROVED, PermissionStatus.OUT)


# =====================================================================================
# 4. NightPermission  (the encapsulated entity)
# =====================================================================================
class NightPermission:
    """
    Encapsulation: all state is private (double underscore). The outside world gets
    read-only properties, and state can only change through validated methods.
    """

    _PHONE_RE = re.compile(r"\+?\d{10,13}")

    def __init__(self, student_id: str, student_name: str, reason: str, destination: str,
                 planned_exit: datetime, expected_return: datetime, guardian_contact: str,
                 policy: NightPolicy, now: datetime):
        reason, destination = (reason or "").strip(), (destination or "").strip()
        guardian_contact = (guardian_contact or "").strip().replace(" ", "")

        if len(reason) < policy.min_reason_length:
            raise ValidationError("Please give a proper reason for the request.")
        if not destination:
            raise ValidationError("Destination is required.")
        if not self._PHONE_RE.fullmatch(guardian_contact):
            raise ValidationError("Enter a valid guardian contact number (10-13 digits).")
        if planned_exit <= now:
            raise ValidationError("Planned exit time must be in the future.")
        if planned_exit > now + timedelta(days=policy.max_advance_days):
            raise ValidationError(f"Requests can be made at most {policy.max_advance_days} days ahead.")
        if not policy.is_night_time(planned_exit.time()):
            raise ValidationError(
                f"Permission is only needed for leaving after {policy.curfew_label}. "
                "Your planned exit is before curfew.")
        if expected_return <= planned_exit:
            raise ValidationError("Return time must be after the exit time.")
        if expected_return - planned_exit > timedelta(hours=policy.max_duration_hours):
            raise ValidationError(f"You can stay out at most {policy.max_duration_hours} hours.")

        self.__policy = policy
        self.__id: Optional[int] = None
        self.__student_id = student_id
        self.__student_name = student_name
        self.__reason = reason
        self.__destination = destination
        self.__planned_exit = planned_exit
        self.__expected_return = expected_return
        self.__guardian_contact = guardian_contact
        self.__status = PermissionStatus.PENDING
        self.__code: Optional[str] = None
        self.__created_at = now
        self.__reviewed_by: Optional[str] = None
        self.__reviewed_at: Optional[datetime] = None
        self.__remarks: Optional[str] = None
        self.__exit_verified_by: Optional[str] = None
        self.__actual_exit: Optional[datetime] = None
        self.__return_verified_by: Optional[str] = None
        self.__actual_return: Optional[datetime] = None
        self.__returned_late = False

    # ---------- read-only properties ----------
    @property
    def id(self) -> int:
        """Database id. Only available once the permission has been saved."""
        if self.__id is None:
            raise NightPermissionError("This permission has not been saved yet, so it has no id.")
        return self.__id
    @property
    def student_id(self): return self.__student_id
    @property
    def student_name(self): return self.__student_name
    @property
    def reason(self): return self.__reason
    @property
    def destination(self): return self.__destination
    @property
    def planned_exit(self): return self.__planned_exit
    @property
    def expected_return(self): return self.__expected_return
    @property
    def guardian_contact(self): return self.__guardian_contact
    @property
    def status(self): return self.__status
    @property
    def remarks(self): return self.__remarks
    @property
    def actual_exit(self): return self.__actual_exit
    @property
    def actual_return(self): return self.__actual_return
    @property
    def returned_late(self): return self.__returned_late

    def attach_id(self, new_id: int) -> None:
        """Database assigns the id exactly once."""
        if self.__id is not None:
            raise NightPermissionError("Permission id is already set.")
        self.__id = new_id

    # ---------- internal helpers ----------
    def __transition(self, new_status: PermissionStatus) -> None:
        if new_status not in _ALLOWED[self.__status]:
            raise InvalidTransitionError(
                f"Cannot change status from {self.__status.value} to {new_status.value}.")
        self.__status = new_status

    def window_overlaps(self, start: datetime, end: datetime) -> bool:
        return start < self.__expected_return and self.__planned_exit < end

    def is_overdue(self, now: datetime) -> bool:
        """Student is outside and has not returned past expected time + grace."""
        grace = timedelta(minutes=self.__policy.return_grace_minutes)
        return self.__status == PermissionStatus.OUT and now > self.__expected_return + grace

    # ---------- authority actions ----------
    def approve(self, authority_id: str, now: datetime) -> None:
        if now >= self.__expected_return:
            raise ValidationError("This request's time window has already passed.")
        self.__transition(PermissionStatus.APPROVED)
        self.__code = f"{secrets.randbelow(10 ** 6):06d}"
        self.__reviewed_by, self.__reviewed_at = authority_id, now

    def reject(self, authority_id: str, remarks: str, now: datetime) -> None:
        if not (remarks or "").strip():
            raise ValidationError("Please give a reason for rejecting.")
        self.__transition(PermissionStatus.REJECTED)
        self.__reviewed_by, self.__reviewed_at = authority_id, now
        self.__remarks = remarks.strip()

    # ---------- student actions ----------
    def cancel(self, student_id: str) -> None:
        if student_id != self.__student_id:
            raise AuthorizationError("You can only cancel your own request.")
        self.__transition(PermissionStatus.CANCELLED)
        self.__code = None

    def get_verification_code(self, student_id: str) -> str:
        """Only the owning student can see the code, and only while it is usable."""
        if student_id != self.__student_id:
            raise AuthorizationError("This code belongs to another student.")
        if self.__status != PermissionStatus.APPROVED or self.__code is None:
            raise InvalidTransitionError("No active verification code for this request.")
        return self.__code

    # ---------- system / security actions ----------
    def expire_if_needed(self, now: datetime) -> bool:
        if self.__status == PermissionStatus.APPROVED and now > self.__expected_return:
            self.__transition(PermissionStatus.EXPIRED)
            self.__code = None
            return True
        return False

    def record_exit(self, guard_id: str, code: str, now: datetime) -> None:
        if self.__status != PermissionStatus.APPROVED:
            raise InvalidTransitionError(f"Exit not allowed - status is {self.__status.value}.")
        earliest = self.__planned_exit - timedelta(minutes=self.__policy.early_exit_minutes)
        if now < earliest:
            raise ValidationError(f"Too early. Exit allowed from {earliest:%I:%M %p}.")
        if now > self.__expected_return:
            raise ValidationError("Permission window is over.")
        if not secrets.compare_digest((code or "").strip(), self.__code or ""):
            raise AuthorizationError("Invalid verification code.")
        self.__transition(PermissionStatus.OUT)
        self.__exit_verified_by, self.__actual_exit = guard_id, now
        self.__code = None  # single use

    def record_return(self, guard_id: str, now: datetime) -> None:
        self.__transition(PermissionStatus.RETURNED)   # only legal from OUT
        grace = timedelta(minutes=self.__policy.return_grace_minutes)
        self.__return_verified_by, self.__actual_return = guard_id, now
        self.__returned_late = now > self.__expected_return + grace

    # ---------- views ----------
    def summary(self) -> dict:
        """Safe public view (never includes the verification code)."""
        return {
            "id": self.__id, "student_id": self.__student_id, "student": self.__student_name,
            "destination": self.__destination, "reason": self.__reason,
            "planned_exit": self.__planned_exit, "expected_return": self.__expected_return,
            "status": self.__status.value, "remarks": self.__remarks,
            "actual_exit": self.__actual_exit, "actual_return": self.__actual_return,
            "returned_late": self.__returned_late,
        }

    def __repr__(self) -> str:
        return (f"<NightPermission #{self.__id} {self.__student_name} "
                f"{self.__planned_exit:%d %b %I:%M %p} -> {self.__status.value}>")

    # ---------- persistence ----------
    def to_record(self) -> dict:
        iso = lambda d: d.isoformat() if d else None
        return {
            "id": self.__id, "student_id": self.__student_id, "student_name": self.__student_name,
            "reason": self.__reason, "destination": self.__destination,
            "planned_exit": iso(self.__planned_exit), "expected_return": iso(self.__expected_return),
            "guardian_contact": self.__guardian_contact, "status": self.__status.value,
            "code": self.__code, "created_at": iso(self.__created_at),
            "reviewed_by": self.__reviewed_by, "reviewed_at": iso(self.__reviewed_at),
            "remarks": self.__remarks, "exit_verified_by": self.__exit_verified_by,
            "actual_exit": iso(self.__actual_exit), "return_verified_by": self.__return_verified_by,
            "actual_return": iso(self.__actual_return), "returned_late": int(self.__returned_late),
        }

    @classmethod
    def from_record(cls, r, policy: NightPolicy) -> "NightPermission":
        dt = lambda s: datetime.fromisoformat(s) if s else None
        obj = cls.__new__(cls)          # skip __init__ validation: data was validated on creation
        obj.__policy = policy
        obj.__id = r["id"]
        obj.__student_id, obj.__student_name = r["student_id"], r["student_name"]
        obj.__reason, obj.__destination = r["reason"], r["destination"]
        obj.__planned_exit, obj.__expected_return = dt(r["planned_exit"]), dt(r["expected_return"])
        obj.__guardian_contact = r["guardian_contact"]
        obj.__status = PermissionStatus(r["status"])
        obj.__code = r["code"]
        obj.__created_at = dt(r["created_at"])
        obj.__reviewed_by, obj.__reviewed_at = r["reviewed_by"], dt(r["reviewed_at"])
        obj.__remarks = r["remarks"]
        obj.__exit_verified_by, obj.__actual_exit = r["exit_verified_by"], dt(r["actual_exit"])
        obj.__return_verified_by, obj.__actual_return = r["return_verified_by"], dt(r["actual_return"])
        obj.__returned_late = bool(r["returned_late"])
        return obj


# =====================================================================================
# 5. SQLITE REPOSITORY
# =====================================================================================
class NightPermissionRepository:
    _COLS = ["student_id", "student_name", "reason", "destination", "planned_exit",
             "expected_return", "guardian_contact", "status", "code", "created_at",
             "reviewed_by", "reviewed_at", "remarks", "exit_verified_by", "actual_exit",
             "return_verified_by", "actual_return", "returned_late"]

    def __init__(self, db_path: str, policy: NightPolicy):
        self._policy = policy
        # check_same_thread=False: Flask serves requests from several threads
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS night_permissions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "student_id TEXT NOT NULL, student_name TEXT NOT NULL, reason TEXT NOT NULL, "
            "destination TEXT NOT NULL, planned_exit TEXT NOT NULL, expected_return TEXT NOT NULL, "
            "guardian_contact TEXT NOT NULL, status TEXT NOT NULL, code TEXT, created_at TEXT, "
            "reviewed_by TEXT, reviewed_at TEXT, remarks TEXT, exit_verified_by TEXT, "
            "actual_exit TEXT, return_verified_by TEXT, actual_return TEXT, "
            "returned_late INTEGER DEFAULT 0)")
        self._conn.commit()

    def save(self, perm: NightPermission) -> None:
        rec = perm.to_record()
        if rec["id"] is None:
            names = ", ".join(self._COLS)
            marks = ", ".join(f":{c}" for c in self._COLS)
            cur = self._conn.execute(f"INSERT INTO night_permissions ({names}) VALUES ({marks})", rec)
            new_id = cur.lastrowid
            if new_id is None:
                raise NightPermissionError("Database did not return an id for the new permission.")
            perm.attach_id(new_id)
        else:
            sets = ", ".join(f"{c} = :{c}" for c in self._COLS)
            self._conn.execute(f"UPDATE night_permissions SET {sets} WHERE id = :id", rec)
        self._conn.commit()

    def _many(self, sql: str, params=()) -> List[NightPermission]:
        rows = self._conn.execute(sql, params).fetchall()
        return [NightPermission.from_record(r, self._policy) for r in rows]

    def get(self, permission_id: int) -> NightPermission:
        rows = self._many("SELECT * FROM night_permissions WHERE id = ?", (permission_id,))
        if not rows:
            raise NotFoundError(f"Permission #{permission_id} not found.")
        return rows[0]

    def by_student(self, student_id: str) -> List[NightPermission]:
        return self._many("SELECT * FROM night_permissions WHERE student_id = ? "
                          "ORDER BY planned_exit DESC", (student_id,))

    def by_status(self, *statuses: PermissionStatus) -> List[NightPermission]:
        marks = ", ".join("?" for _ in statuses)
        return self._many(f"SELECT * FROM night_permissions WHERE status IN ({marks}) "
                          "ORDER BY planned_exit", [s.value for s in statuses])


# =====================================================================================
# 6. USER STUBS  (replace with Shivansh's User hierarchy when ready)
# =====================================================================================
class User:
    def __init__(self, user_id: str, name: str):
        self.user_id, self.name = user_id, name

    def view_dashboard(self, service: "NightPermissionService") -> str:   # polymorphism hook
        raise NotImplementedError


class Student(User):
    def view_dashboard(self, service):
        rows = service.my_requests(self)
        lines = [f"[Student] {self.name} - your night permissions"]
        lines += [f"  #{p.id} {p.planned_exit:%d %b %I:%M %p} -> {p.destination}: {p.status.value}"
                  for p in rows] or ["  (none)"]
        return "\n".join(lines)


class Authority(User):
    def view_dashboard(self, service):
        rows = service.pending_requests(self)
        lines = [f"[Authority] {self.name} - {len(rows)} pending request(s)"]
        lines += [f"  #{p.id} {p.student_name} ({p.student_id}) {p.planned_exit:%d %b %I:%M %p}"
                  f" -> {p.destination}" for p in rows]
        return "\n".join(lines)


class SecurityGuard(User):
    def view_dashboard(self, service):
        exp, out = service.expected_tonight(self), service.students_outside(self)
        lines = [f"[Guard] {self.name} - {len(exp)} expected tonight, {len(out)} outside now"]
        lines += [f"  EXPECTED #{p.id} {p.student_name} at {p.planned_exit:%I:%M %p}" for p in exp]
        lines += [f"  OUTSIDE  #{p.id} {p.student_name} due {p.expected_return:%I:%M %p}"
                  + ("  ** OVERDUE **" if p.is_overdue(service.now()) else "") for p in out]
        return "\n".join(lines)


# =====================================================================================
# 7. SERVICE LAYER  (role checks + business rules; the Flask routes will call this)
# =====================================================================================
class NightPermissionService:
    def __init__(self, db_path: str = "campus.db", policy: Optional[NightPolicy] = None,
                 clock: Callable[[], datetime] = ist_now):
        self.policy = policy or NightPolicy()
        self.repo = NightPermissionRepository(db_path, self.policy)
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _require(user: User, role: type, action: str) -> None:
        if not isinstance(user, role):
            raise AuthorizationError(f"Only {role.__name__} can {action}.")

    def expire_stale(self) -> int:
        """Mark unused approvals whose window has passed. Call on each request / by a timer."""
        count = 0
        for p in self.repo.by_status(PermissionStatus.APPROVED):
            if p.expire_if_needed(self.now()):
                self.repo.save(p)
                count += 1
        return count

    # ---------- student ----------
    def submit_request(self, student: Student, reason: str, destination: str,
                       planned_exit: datetime, expected_return: datetime,
                       guardian_contact: str) -> NightPermission:
        self._require(student, Student, "submit night permission requests")
        self.expire_stale()
        perm = NightPermission(student.user_id, student.name, reason, destination,
                               planned_exit, expected_return, guardian_contact,
                               self.policy, self.now())
        for other in self.repo.by_student(student.user_id):      # no double-booking yourself
            if other.status in _ACTIVE and other.window_overlaps(planned_exit, expected_return):
                raise ValidationError(f"Overlaps with your existing request #{other.id}.")
        self.repo.save(perm)
        return perm

    def my_requests(self, student: Student) -> List[NightPermission]:
        self._require(student, Student, "view their own requests")
        self.expire_stale()
        return self.repo.by_student(student.user_id)

    def cancel_request(self, student: Student, permission_id: int) -> NightPermission:
        self._require(student, Student, "cancel requests")
        perm = self.repo.get(permission_id)
        perm.cancel(student.user_id)
        self.repo.save(perm)
        return perm

    def get_my_code(self, student: Student, permission_id: int) -> str:
        self._require(student, Student, "view verification codes")
        return self.repo.get(permission_id).get_verification_code(student.user_id)

    # ---------- authority ----------
    def pending_requests(self, authority: Authority) -> List[NightPermission]:
        self._require(authority, Authority, "view pending requests")
        return self.repo.by_status(PermissionStatus.PENDING)

    def approve(self, authority: Authority, permission_id: int) -> NightPermission:
        self._require(authority, Authority, "approve requests")
        perm = self.repo.get(permission_id)
        perm.approve(authority.user_id, self.now())
        self.repo.save(perm)
        return perm

    def reject(self, authority: Authority, permission_id: int, remarks: str) -> NightPermission:
        self._require(authority, Authority, "reject requests")
        perm = self.repo.get(permission_id)
        perm.reject(authority.user_id, remarks, self.now())
        self.repo.save(perm)
        return perm

    # ---------- security guard ----------
    def expected_tonight(self, guard: SecurityGuard) -> List[NightPermission]:
        self._require(guard, SecurityGuard, "view the gate list")
        self.expire_stale()
        today: date = self.now().date()
        return [p for p in self.repo.by_status(PermissionStatus.APPROVED)
                if p.planned_exit.date() == today]

    def students_outside(self, guard: SecurityGuard) -> List[NightPermission]:
        self._require(guard, SecurityGuard, "view students outside")
        return self.repo.by_status(PermissionStatus.OUT)

    def overdue(self, guard: SecurityGuard) -> List[NightPermission]:
        return [p for p in self.students_outside(guard) if p.is_overdue(self.now())]

    def verify_exit(self, guard: SecurityGuard, permission_id: int, code: str) -> NightPermission:
        self._require(guard, SecurityGuard, "verify exits")
        self.expire_stale()
        perm = self.repo.get(permission_id)
        perm.record_exit(guard.user_id, code, self.now())
        self.repo.save(perm)
        return perm

    def verify_return(self, guard: SecurityGuard, permission_id: int) -> NightPermission:
        self._require(guard, SecurityGuard, "verify returns")
        perm = self.repo.get(permission_id)
        perm.record_return(guard.user_id, self.now())
        self.repo.save(perm)
        return perm


# =====================================================================================
# 8. DEMO  -  run:  python night_permissions.py
# =====================================================================================
class _DemoClock:
    def __init__(self, start: datetime): self.t = start
    def __call__(self): return self.t


def _demo() -> None:
    clock = _DemoClock(datetime(2026, 11, 22, 18, 0))
    svc = NightPermissionService(":memory:", NightPolicy(), clock)

    student = Student("1025250199", "Demo Student")
    warden = Authority("AUTH01", "Warden Sharma")
    guard = SecurityGuard("SEC01", "Guard Singh")

    print(f"Curfew is {svc.policy.curfew_label}\n")

    print("1) Request that leaves BEFORE curfew (should fail):")
    try:
        svc.submit_request(student, "Dinner with friends", "Sector 17 Market",
                           datetime(2026, 11, 22, 20, 0), datetime(2026, 11, 22, 23, 0), "9876543210")
    except ValidationError as e:
        print("   Rejected by system ->", e)

    print("\n2) Valid request (leave 10:30 PM, back by 1:00 AM):")
    perm = svc.submit_request(student, "Cousin's wedding function", "City Palace Banquet",
                              datetime(2026, 11, 22, 22, 30), datetime(2026, 11, 23, 1, 0), "9876543210")
    print("  ", perm)
    print(warden.view_dashboard(svc))

    print("\n3) Authority approves; student gets the code:")
    svc.approve(warden, perm.id)
    code = svc.get_my_code(student, perm.id)
    print("  ", perm.id, "-> code", code)

    print("\n4) At the gate, 10:25 PM:")
    clock.t = datetime(2026, 11, 22, 22, 25)
    print(guard.view_dashboard(svc))
    try:
        svc.verify_exit(guard, perm.id, "000000")
    except AuthorizationError as e:
        print("   Wrong code ->", e)
    print("   Correct code ->", svc.verify_exit(guard, perm.id, code))

    print("\n5) Student returns at 1:30 AM (late):")
    clock.t = datetime(2026, 11, 23, 1, 30)
    print(guard.view_dashboard(svc))
    done = svc.verify_return(guard, perm.id)
    print("   Returned late?", done.returned_late)
    print(student.view_dashboard(svc))

    print("\n6) Student tries to approve own request (role check):")
    try:
        svc.approve(student, perm.id)  # type: ignore[arg-type]  # intentional: shows the role check
    except AuthorizationError as e:
        print("   Blocked ->", e)


if __name__ == "__main__":
    _demo()