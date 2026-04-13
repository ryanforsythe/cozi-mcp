#!/usr/bin/env python3
"""
Cozi MCP Server
Manage the Forsythe family Cozi calendar and lists via the Cozi REST API.
"""

import json
import os
import sys
import aiohttp
from typing import Optional
from loguru import logger
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
# Stderr only — stdout is reserved for the MCP stdio protocol.
# File logging goes to /logs/cozi-mcp.log (mount a host dir to /logs to persist).
logger.remove()
logger.add(sys.stderr, level="DEBUG", format="{time:HH:mm:ss} | {level} | {message}")
logger.add(
    "/logs/cozi-mcp.log",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    rotation="10 MB",
    retention=3,
    catch=True,  # don't crash if /logs doesn't exist
)

# ── Server ────────────────────────────────────────────────────────────────────
mcp = FastMCP("cozi_mcp")

# ── Config ────────────────────────────────────────────────────────────────────
COZI_USERNAME = os.getenv("COZI_USERNAME", "")
COZI_PASSWORD = os.getenv("COZI_PASSWORD", "")

if not COZI_USERNAME or not COZI_PASSWORD:
    raise RuntimeError("COZI_USERNAME and COZI_PASSWORD must be set in .env or environment")

URL_BASE       = "https://rest.cozi.com"
URL_LOGIN      = f"{URL_BASE}/api/ext/2207/auth/login?apikey=coziwc|v256_production"
URL_PERSON     = f"{URL_BASE}/api/ext/2004/{{acct}}/account/person/"
URL_CALENDAR   = f"{URL_BASE}/api/ext/2004/{{acct}}/calendar/{{year}}/{{month}}"
URL_LISTS      = f"{URL_BASE}/api/ext/2004/{{acct}}/list/"
URL_LIST       = f"{URL_BASE}/api/ext/2004/{{acct}}/list/{{list_id}}"
URL_LIST_ITEMS = f"{URL_BASE}/api/ext/2004/{{acct}}/list/{{list_id}}/item/"

# Fallback UUIDs used if the persons API is unavailable.
_FALLBACK_MEMBERS: dict[str, str] = {
    "ryan":      "d1f556b7-f156-4ef4-ba99-7a362d539a7b",
    "veronica":  "f172ce69-2416-4233-9ec4-298028cc8f9b",
    "alexandra": "307a427d-a415-46e4-a35d-81d6270b2dfc",
    "taryn":     "c377926c-e7d0-48c9-94d0-061fbe2fa383",
    "connor":    "195e3e17-f62a-402a-9491-529ea9cec347",
    "elizabeth": "34ae0658-3d22-453d-9968-08434e1049da",
    "gretchen":  "03b4ef4e-9358-43a1-948f-875693045445",
    "djena":     "bd69787f-cc7c-4506-85ce-d50468663483",
}

# In-process persons cache: account_id -> list of {personId, email, name}
_persons_cache: dict[str, list[dict]] = {}


# ── Shared helpers ────────────────────────────────────────────────────────────

async def _login() -> tuple[str, str]:
    """Login and return (access_token, account_id)."""
    async with aiohttp.ClientSession() as session:
        async with session.post(URL_LOGIN, json={
            "username": COZI_USERNAME,
            "password": COZI_PASSWORD,
            "issueRefresh": True,
        }) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Cozi login failed (HTTP {resp.status}): {text}")
            data = await resp.json()
            return data["accessToken"], data["accountId"]


async def _raise_for_status(resp, body: str) -> None:
    """Raise with response body included so errors are actionable."""
    if resp.status >= 400:
        raise RuntimeError(f"Cozi API error (HTTP {resp.status}): {body}")


async def _get(url: str, token: str) -> dict:
    logger.debug("GET {}", url)
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as s:
        async with s.get(url) as resp:
            body = await resp.text()
            logger.debug("GET {} → {} {}", url, resp.status, body[:500])
            await _raise_for_status(resp, body)
            return json.loads(body)


async def _post(url: str, token: str, payload) -> dict:
    logger.debug("POST {} payload={}", url, json.dumps(payload, indent=2))
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as s:
        async with s.post(url, json=payload) as resp:
            body = await resp.text()
            logger.debug("POST {} → {} {}", url, resp.status, body[:500])
            await _raise_for_status(resp, body)
            return json.loads(body) if body.strip() else {}


async def _patch(url: str, token: str, payload) -> dict:
    logger.debug("PATCH {} payload={}", url, json.dumps(payload, indent=2))
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as s:
        async with s.patch(url, json=payload) as resp:
            body = await resp.text()
            logger.debug("PATCH {} → {} {}", url, resp.status, body[:500])
            await _raise_for_status(resp, body)
            return await resp.json()


def _z(n: int) -> str:
    """Zero-pad a number to 2 digits."""
    return str(n).zfill(2)


def _default_end_time(start_time: str) -> str:
    """Return start_time + 1 hour, capped at 23:59. Returns '' if start_time is empty."""
    if not start_time:
        return ""
    h, m = map(int, start_time.split(":"))
    h += 1
    if h >= 24:
        return start_time
    return f"{_z(h)}:{_z(m)}"


def _is_uuid(s: str) -> bool:
    return len(s) == 36 and s.count("-") == 4


async def _fetch_persons(token: str, acct: str) -> list[dict]:
    """Fetch persons from API, update cache, and return list."""
    data = await _get(URL_PERSON.format(acct=acct), token)
    raw = data if isinstance(data, list) else data.get("persons", [])
    persons = [
        {
            "personId": p.get("accountPersonId"),
            "email":    p.get("email") or None,
            "name":     p.get("name", ""),
        }
        for p in raw
        if p.get("accountPersonId")
    ]
    _persons_cache[acct] = persons
    return persons


async def _get_persons(token: str, acct: str) -> list[dict]:
    """Return cached persons, fetching from API on first call."""
    if acct not in _persons_cache:
        return await _fetch_persons(token, acct)
    return _persons_cache[acct]


async def _resolve_persons(
    names: Optional[list[str]], token: str, acct: str
) -> list[str]:
    """
    Resolve a list of names/UUIDs to UUIDs.
    If names is None/empty, returns all family member UUIDs.
    Already-valid UUIDs are passed through unchanged.
    """
    persons = await _get_persons(token, acct)
    name_map: dict[str, str] = {
        p["name"].strip().lower(): p["personId"] for p in persons
    }
    # Merge fallback so short names still resolve if API shape differs
    for k, v in _FALLBACK_MEMBERS.items():
        name_map.setdefault(k, v)

    if not names:
        return [p["personId"] for p in persons]

    uuids = []
    for n in names:
        if _is_uuid(n):
            uuids.append(n)
        else:
            resolved = name_map.get(n.strip().lower())
            if resolved:
                uuids.append(resolved)
            # silently skip unrecognised names rather than passing junk to API
    return uuids


def _notify_uuids(attendee_uuids: list[str], persons: list[dict]) -> list[str]:
    """Return the subset of attendee UUIDs that have a non-None email (can receive notifications)."""
    emailable = {p["personId"] for p in persons if p.get("email")}
    return [uid for uid in attendee_uuids if uid in emailable]


# ── Calendar tools ────────────────────────────────────────────────────────────

@mcp.tool(name="cozi_get_calendar", annotations={"readOnlyHint": True, "destructiveHint": False})
async def cozi_get_calendar(year: int, month: int) -> str:
    """
    Get all Cozi calendar appointments for a given month.

    Args:
        year:  4-digit year, e.g. 2026
        month: Month number 1–12

    Returns:
        Formatted list of appointments with date, time, subject, location, and notes.
    """
    token, acct = await _login()
    data = await _get(URL_CALENDAR.format(acct=acct, year=year, month=_z(month)), token)

    appointments = data.get("appointments", [])
    if not appointments:
        return f"No appointments found for {year}-{_z(month)}."

    lines = [f"📅 Cozi Calendar — {year}/{_z(month)}\n"]
    for appt in sorted(appointments, key=lambda a: a.get("startDay", "")):
        details  = appt.get("details", {})
        subject  = details.get("subject", "(no title)")
        start    = appt.get("startDay", "")
        s_time   = details.get("startTime", "")
        e_time   = details.get("endTime", "")
        location = details.get("location", "")
        appt_id  = appt.get("id", "")
        time_str = f"{s_time}–{e_time}" if s_time else "All day"
        loc_str  = f" @ {location}" if location else ""
        lines.append(f"• [{appt_id}] {start} {time_str}  **{subject}**{loc_str}")

    return "\n".join(lines)


async def _do_add_appointment(
    token: str, acct: str,
    subject: str, year: int, month: int, day: int,
    start_time: str, end_time: str, location: str, notes: str,
    attendees: Optional[list[str]], notify_persons: Optional[list[str]],
    reminder_minutes: int,
) -> str:
    persons        = await _get_persons(token, acct)
    attendee_uuids = await _resolve_persons(attendees, token, acct)
    if notify_persons is not None:
        notify_uuids = await _resolve_persons(notify_persons, token, acct)
    else:
        notify_uuids = _notify_uuids(attendee_uuids, persons)

    # Cozi requires end_time when start_time is set; default to +1 hour
    if start_time and not end_time:
        end_time = _default_end_time(start_time)

    start_day = f"{year}-{_z(month)}-{_z(day)}"
    payload_item = {
        "itemType": "appointment",
        "notifyPersons": notify_uuids,
        "create": {
            "startDay": start_day,
            "details": {
                "startTime": start_time,
                "endTime":   end_time,
                "dateSpan":  0,
                "attendeeSet": attendee_uuids,
                "location":  location,
                "notes":     notes,
                "subject":   subject,
            },
        },
    }
    if reminder_minutes > 0:
        payload_item["create"]["reminders"] = [{"minutesBefore": reminder_minutes}]

    await _post(URL_CALENDAR.format(acct=acct, year=year, month=_z(month)), token, [payload_item])

    time_str = f" at {start_time}–{end_time}" if start_time else " (all day)"
    loc_str  = f" @ {location}" if location else ""
    return f"✅ Added **{subject}** to Cozi on {start_day}{time_str}{loc_str}"


@mcp.tool(name="cozi_add_appointment", annotations={"readOnlyHint": False, "destructiveHint": False})
async def cozi_add_appointment(
    subject: str,
    year: int,
    month: int,
    day: int,
    start_time: str = "",
    end_time: str = "",
    location: str = "",
    notes: str = "",
    attendees: Optional[list[str]] = None,
    notify_persons: Optional[list[str]] = None,
    reminder_minutes: int = 30,
) -> str:
    """
    Add an appointment to the Cozi family calendar.

    Args:
        subject:          Event title, e.g. "Soccer Practice"
        year:             4-digit year
        month:            Month number 1–12
        day:              Day of month
        start_time:       24-hour "HH:MM", or "" for all-day
        end_time:         24-hour "HH:MM". Required when start_time is set; defaults
                          to start_time + 1 hour if omitted. Use "" for all-day.
        location:         Optional venue/location string
        notes:            Optional free-text notes about the event itself. Do NOT
                          put attendee or notification info here.
        attendees:        List of person UUIDs. Call cozi_get_persons first to
                          resolve names to UUIDs. Defaults to all family members.
        notify_persons:   List of person UUIDs to notify. Defaults to all attendees
                          who have an email address. Call cozi_get_persons to resolve
                          names to UUIDs.
        reminder_minutes: Minutes before event to remind. Use 0 to skip.

    Returns:
        Confirmation string on success.
    """
    token, acct = await _login()
    try:
        return await _do_add_appointment(
            token, acct, subject, year, month, day,
            start_time, end_time, location, notes,
            attendees, notify_persons, reminder_minutes,
        )
    except Exception:
        # Refresh persons cache in case UUIDs changed, then retry once
        await _fetch_persons(token, acct)
        return await _do_add_appointment(
            token, acct, subject, year, month, day,
            start_time, end_time, location, notes,
            attendees, notify_persons, reminder_minutes,
        )


@mcp.tool(name="cozi_delete_appointment", annotations={"readOnlyHint": False, "destructiveHint": True})
async def cozi_delete_appointment(appointment_id: str, year: int, month: int) -> str:
    """
    Delete a Cozi calendar appointment by its ID.

    Args:
        appointment_id: The appointment ID (visible in cozi_get_calendar output)
        year:           Year of the appointment
        month:          Month of the appointment

    Returns:
        Confirmation string on success.
    """
    token, acct = await _login()
    payload = [{"itemType": "appointment", "delete": {"id": appointment_id}}]
    await _post(URL_CALENDAR.format(acct=acct, year=year, month=_z(month)), token, payload)
    return f"✅ Deleted appointment {appointment_id} from Cozi."


# ── List tools ────────────────────────────────────────────────────────────────

@mcp.tool(name="cozi_get_lists", annotations={"readOnlyHint": True, "destructiveHint": False})
async def cozi_get_lists() -> str:
    """
    Get all Cozi lists (shopping, to-do, etc.) with their IDs and current items.

    Returns:
        Formatted summary of every list and its items.
    """
    token, acct = await _login()
    data = await _get(URL_LISTS.format(acct=acct), token)

    lists = data.get("lists", [])
    if not lists:
        return "No lists found in Cozi."

    lines = ["📋 Cozi Lists\n"]
    for lst in lists:
        title   = lst.get("title", "(untitled)")
        list_id = lst.get("listId", "")
        items   = lst.get("items", [])
        lines.append(f"**{title}** (ID: `{list_id}`) — {len(items)} items")
        for item in items[:10]:
            status = " ✓" if item.get("status") == "complete" else ""
            lines.append(f"  • [{item.get('itemId', '')}] {item.get('text', '')}{status}")
        if len(items) > 10:
            lines.append(f"  … and {len(items) - 10} more")

    return "\n".join(lines)


@mcp.tool(name="cozi_add_list_item", annotations={"readOnlyHint": False, "destructiveHint": False})
async def cozi_add_list_item(list_id: str, text: str, position: int = 0) -> str:
    """
    Add an item to a Cozi list.

    Args:
        list_id:  The list ID (from cozi_get_lists)
        text:     Item text to add
        position: Position in list (0 = top)

    Returns:
        Confirmation string on success.
    """
    token, acct = await _login()
    await _post(URL_LIST_ITEMS.format(acct=acct, list_id=list_id), token, {"text": text, "position": position})
    return f"✅ Added **{text}** to Cozi list."


@mcp.tool(name="cozi_remove_list_items", annotations={"readOnlyHint": False, "destructiveHint": True})
async def cozi_remove_list_items(list_id: str, item_ids: list[str]) -> str:
    """
    Remove one or more items from a Cozi list.

    Args:
        list_id:  The list ID (from cozi_get_lists)
        item_ids: Item IDs to remove (from cozi_get_lists output)

    Returns:
        Confirmation string on success.
    """
    token, acct = await _login()
    operations = [{"op": "remove", "path": f"/items/{i}"} for i in item_ids]
    await _patch(URL_LIST.format(acct=acct, list_id=list_id), token, {"operations": operations})
    return f"✅ Removed {len(item_ids)} item(s) from Cozi list."


# ── Persons tool ──────────────────────────────────────────────────────────────

@mcp.tool(name="cozi_get_persons", annotations={"readOnlyHint": True, "destructiveHint": False})
async def cozi_get_persons() -> str:
    """
    Get all family members in the Cozi account with their UUIDs and email addresses.
    Use the returned personId UUIDs directly in attendees/notify_persons fields.

    Returns:
        List of family members with personId, name, and email.
    """
    token, acct = await _login()
    persons = await _fetch_persons(token, acct)  # always refresh when called explicitly

    if not persons:
        return "No family members found."

    lines = ["👨‍👩‍👧‍👦 Cozi Family Members\n"]
    for p in persons:
        email_str = p["email"] if p["email"] else "(no email — notifications not sent)"
        lines.append(f"• {p['name']} — `{p['personId']}` — {email_str}")

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="stdio")
