from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import httpx
from dateutil import parser as dparser
from zoneinfo import ZoneInfo
import datetime
from typing import Any, Dict, List, Tuple, Optional

# WHY: Always use the UK timezone so DST is handled by the system zone database.
LONDON = ZoneInfo("Europe/London")

app = FastAPI()
API_BASE = "https://api.satchelone.com/api"
HTTP_TIMEOUT_SECONDS = 15  # WHY: small timeout so widget requests fail fast rather than hang forever


# WHY: Convert input strings to aware datetimes in London; for date-only strings treat them specially.
def parse_dt_to_london(s: Optional[str]) -> Optional[datetime.datetime]:
    if not s:
        return None
    s = s.strip()
    # Fast path for date-only strings like YYYY-MM-DD
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            y, m, d = (int(x) for x in s.split("-"))
            # WHY: Treat date-only due dates as end-of-day in London so they count for the whole day.
            return datetime.datetime(y, m, d, 23, 59, 59, tzinfo=LONDON)
        except Exception:
            pass
    try:
        dt = dparser.parse(s)
    except Exception:
        return None
    # WHY: If no tz info provided assume UTC (best-effort) then convert to London.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(LONDON)


# WHY: Make sure the Authorization header is normalized into a Bearer token if user omitted the prefix.
def make_auth_header(auth_value: str) -> str:
    a = (auth_value or "").strip()
    if not a:
        return ""
    if a.lower().startswith("bearer "):
        return a
    return "Bearer " + a


# WHY: Create succinct lesson fields for the widget; widget will read these individual keys.
def extract_lesson_fields(lesson: Dict[str, Any]) -> Dict[str, str]:
    subject = lesson.get("classGroup", {}).get("subject") or lesson.get("subject") or "No Lesson"
    start_raw = lesson.get("period", {}).get("startDateTime") or "00:00"
    end_raw = lesson.get("period", {}).get("endDateTime") or "00:00"
    room = lesson.get("room") or "0"
    teacher = ""
    t = lesson.get("teacher") if t != "" else "No Teacher"
    if isinstance(t, dict):
        teacher = " ".join([v for v in (t.get("title"), t.get("forename"), t.get("surname")) if v]).strip()
    return {
        "subject": subject,
        "start_raw": start_raw,
        "end_raw": end_raw,
        "room": room,
        "teacher": teacher,
    }


# WHY: Format a timezone-aware datetime to HH:MM local time for the widget.
def fmt_hm(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return ""
    return dt.strftime("%H:%M")


@app.get("/widget")
def widget(request: Request):
    """
    Expected headers:
      - Authorization: <token or 'Bearer TOKEN'>
      - X-User-Id: <numeric user id>
      - X-School-Id: <numeric school id>

    Returns flat, top-level JSON with:
      - now_hm, next_change_hm, refresh_seconds
      - current_lesson_subject, current_lesson_start_hm, current_lesson_end_hm, current_lesson_room, current_lesson_teacher
      - next_lesson_subject, next_lesson_start_hm, next_lesson_end_hm, next_lesson_room, next_lesson_teacher
      - hw_1_title, hw_1_subject, hw_1_due_date (YYYY-MM-DD), hw_1_due_time_hm (HH:MM or "")
      - hw_2_..., hw_3_..., homework_count, status/message
    """
    # WHY: Accept common header casings for robustness across clients.
    raw_auth = request.headers.get("authorization") or request.headers.get("Authorization")
    user_id = (
        request.headers.get("x-user-id")
        or request.headers.get("X-User-Id")
        or request.headers.get("user-id")
    )
    school_id = (
        request.headers.get("x-school-id")
        or request.headers.get("X-School-Id")
        or request.headers.get("school-id")
    )

    if not raw_auth or not user_id or not school_id:
        raise HTTPException(status_code=400, detail="Missing required headers: Authorization, X-User-Id, X-School-Id")

    headers = {
        "Accept": "application/smhw.v2021.5+json",
        "Authorization": make_auth_header(raw_auth),
        "Connection": "keep-alive",
    }

    client = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    now = datetime.datetime.now(tz=LONDON)  # WHY: Use London-local now for all comparisons

    # Base response
    resp: Dict[str, Any] = {"status": "ok", "now_hm": fmt_hm(now)}

    # Candidate change times (tz-aware London datetimes) used to compute next_change_hm and refresh_seconds
    candidates: List[datetime.datetime] = []

    # --- Timetable: fetch today's lessons only ---
    try:
        tt_resp = client.get(f"{API_BASE}/timetable/school/{school_id}/student/{user_id}", headers=headers)
        tt_resp.raise_for_status()
        tt_json = tt_resp.json()
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"Failed to fetch timetable: {str(e)}"}, status_code=502)

    weeks = tt_json.get("weeks", []) or []
    today_day = None
    today_str = now.date().isoformat()
    if weeks:
        # WHY: Usually the API returns weeks->[days], take the first week and find today's entry.
        for d in weeks[0].get("days", []) or []:
            if d.get("date") == today_str:
                today_day = d
                break

    current_lesson_fields: Dict[str, str] = {}
    next_lesson_fields: Dict[str, str] = {}

    if today_day:
        lessons = today_day.get("lessons", []) or []
        parsed: List[Tuple[datetime.datetime, datetime.datetime, Dict[str, Any]]] = []
        for lesson in lessons:
            start_s = lesson.get("period", {}).get("startDateTime")
            end_s = lesson.get("period", {}).get("endDateTime")
            if not start_s or not end_s:
                continue
            start_dt = parse_dt_to_london(start_s)
            end_dt = parse_dt_to_london(end_s)
            if start_dt is None or end_dt is None:
                continue
            parsed.append((start_dt, end_dt, lesson))
        parsed.sort(key=lambda t: t[0])

        for start_dt, end_dt, lesson in parsed:
            if start_dt <= now <= end_dt and not current_lesson_fields:
                f = extract_lesson_fields(lesson)
                current_lesson_fields = {
                    "current_lesson_subject": f["subject"],
                    "current_lesson_start_hm": fmt_hm(start_dt),
                    "current_lesson_end_hm": fmt_hm(end_dt),
                    "current_lesson_room": f["room"],
                    "current_lesson_teacher": f["teacher"],
                }
            if start_dt > now and not next_lesson_fields:
                f = extract_lesson_fields(lesson)
                next_lesson_fields = {
                    "next_lesson_subject": f["subject"],
                    "next_lesson_start_hm": fmt_hm(start_dt),
                    "next_lesson_end_hm": fmt_hm(end_dt),
                    "next_lesson_room": f["room"],
                    "next_lesson_teacher": f["teacher"],
                }
            # Collect future starts/ends for next_change calculation
            if start_dt > now:
                candidates.append(start_dt)
            if end_dt > now:
                candidates.append(end_dt)

    # Ensure keys exist (Widgy expects stable keys)
    defaults = {
        "current_lesson_subject": "", "current_lesson_start_hm": "", "current_lesson_end_hm": "",
        "current_lesson_room": "", "current_lesson_teacher": "",
        "next_lesson_subject": "", "next_lesson_start_hm": "", "next_lesson_end_hm": "",
        "next_lesson_room": "", "next_lesson_teacher": "",
    }
    resp.update({k: v for k, v in defaults.items()})
    resp.update(current_lesson_fields)
    resp.update(next_lesson_fields)

    # --- Homework: fetch and filter only future-or-now due items ---
    try:
        hw_resp = client.get(f"{API_BASE}/personal_calendar_tasks", headers=headers)
        hw_resp.raise_for_status()
        hw_json = hw_resp.json()
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": f"Failed to fetch homework: {str(e)}"}, status_code=502)

    tasks = hw_json.get("personal_calendar_tasks", []) or []
    filtered: List[Tuple[datetime.datetime, Dict[str, Any], bool]] = []
    for t in tasks:
        due_raw = t.get("due_on") or t.get("due") or t.get("date") or t.get("dueDate")
        if not due_raw:
            continue
        # Detect date-only strings so we can preserve whether the original was date-only
        is_date_only = isinstance(due_raw, str) and len(due_raw) == 10 and due_raw[4] == "-" and due_raw[7] == "-"
        dt = parse_dt_to_london(due_raw)
        if dt and dt >= now:
            filtered.append((dt, t, is_date_only))
            candidates.append(dt)

    filtered.sort(key=lambda x: x[0])
    top_three = [entry for entry in filtered][:3]

    # Flatten homework into hw_1..hw_3 with separate date and time fields
    for i in range(3):
        base = f"hw_{i+1}"
        if i < len(top_three):
            dt, t, is_date_only = top_three[i]
            title = t.get("class_task_title") or t.get("title") or ""
            subject = t.get("subject") or ""
            due_date = dt.date().isoformat()
            due_time_hm = "" if is_date_only else fmt_hm(dt)
            resp[f"{base}_title"] = title
            resp[f"{base}_subject"] = subject
            resp[f"{base}_due_date"] = due_date
            resp[f"{base}_due_time_hm"] = due_time_hm
        else:
            resp[f"{base}_title"] = ""
            resp[f"{base}_subject"] = ""
            resp[f"{base}_due_date"] = ""
            resp[f"{base}_due_time_hm"] = ""

    resp["homework_count"] = len(top_three)

    # --- next change and refresh hint ---
    if candidates:
        next_change_at = min(candidates)
        refresh_seconds = int((next_change_at - now).total_seconds())
        if refresh_seconds < 5:
            refresh_seconds = 5
        if refresh_seconds > 3600:
            refresh_seconds = 3600
        resp["next_change_hm"] = fmt_hm(next_change_at)
        resp["refresh_seconds"] = refresh_seconds
    else:
        resp["next_change_hm"] = ""
        resp["refresh_seconds"] = 300

    # WHY: Use no-store to reduce stale caching between widget polls.
    return JSONResponse(
        content=resp,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
