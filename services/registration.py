import asyncio
import base64
import io
import json
import uuid
from datetime import datetime, timezone

import qrcode
from fastapi import HTTPException

from config.supabase import supabase_admin


async def get_user_registrations(user_qr_code: str) -> list[dict]:
    """Fetch all registrations for a user, merged with event details."""
    try:
        reg_res = await (
            supabase_admin.table("registrations")
            .select("id, event_id, registered_at, attended_at")
            .eq("user_qr_code", user_qr_code)
            .order("registered_at")
            .execute()
        )
        registrations = reg_res.data or []
        if not registrations:
            return []

        event_ids = list({r["event_id"] for r in registrations})
        events_res = await (
            supabase_admin.table("events")
            .select("id, title, description, location, start_time, end_time, is_active")
            .in_("id", event_ids)
            .execute()
        )
        events_by_id = {str(e["id"]): e for e in (events_res.data or [])}

        return [{**reg, "event": events_by_id.get(str(reg["event_id"]), {})} for reg in registrations]
    except Exception:
        return []


async def get_available_events_for_user(user_qr_code: str) -> list[dict]:
    """Returns active events the user is NOT already registered for."""
    try:
        events_res, reg_res = await asyncio.gather(
            supabase_admin.table("events")
            .select("id, title, description, location, start_time, end_time")
            .eq("is_active", True)
            .execute(),
            supabase_admin.table("registrations")
            .select("event_id")
            .eq("user_qr_code", user_qr_code)
            .execute(),
        )
        active_events = events_res.data or []
        registered_ids = {r["event_id"] for r in (reg_res.data or [])}
        return [e for e in active_events if str(e["id"]) not in registered_ids]
    except Exception:
        return []


async def register_for_event(
        user_qr_code: str,
        event_id: str,
        user_name: str,
        user_email: str,
) -> dict:
    """
    Creates a registration row and generates a unique QR for this event.
    Returns dict with registration id and qr_data_url.
    """
    reg_id = str(uuid.uuid4())

    try:
        res = await (
            supabase_admin.table("registrations")
            .insert({"id": reg_id, "user_qr_code": user_qr_code, "event_id": event_id})
            .execute()
        )
        registration = res.data[0]
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            raise HTTPException(status_code=409, detail="Already registered for this event.")
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")

    # Get event title for QR payload
    try:
        event_res = await (
            supabase_admin.table("events")
            .select("title")
            .eq("id", event_id)
            .single()
            .execute()
        )
        event_title = event_res.data.get("title", "FOSSUoK Event") if event_res.data else "FOSSUoK Event"
    except Exception:
        event_title = "FOSSUoK Event"

    qr_payload = json.dumps(
        {"rid": reg_id, "uid": user_qr_code, "eid": event_id, "name": user_name, "event": event_title},
        separators=(",", ":"),
    )
    qr_data_url = await asyncio.to_thread(_generate_qr_data_url, qr_payload)

    return {**registration, "qr_data_url": qr_data_url, "event_title": event_title}


async def get_registration_qr_payload(registration_id: str, user_qr_code: str) -> str | None:
    """Rebuilds the QR payload for a registration (for download/display)."""
    try:
        reg_res = await (
            supabase_admin.table("registrations")
            .select("id, user_qr_code, event_id")
            .eq("id", registration_id)
            .eq("user_qr_code", user_qr_code)  # ownership check
            .single()
            .execute()
        )
        if not reg_res.data:
            return None
        reg = reg_res.data

        event_res = await (
            supabase_admin.table("events")
            .select("title")
            .eq("id", reg["event_id"])
            .single()
            .execute()
        )
        event_title = event_res.data.get("title", "FOSSUoK Event") if event_res.data else "FOSSUoK Event"

        # Need username too
        user_res = await (
            supabase_admin.table("users")
            .select("name")
            .eq("qr_code_data", user_qr_code)
            .single()
            .execute()
        )
        user_name = user_res.data.get("name", "") if user_res.data else ""

        return json.dumps(
            {"rid": reg["id"], "uid": reg["user_qr_code"], "eid": reg["event_id"],
             "name": user_name, "event": event_title},
            separators=(",", ":"),
        )
    except Exception:
        return None


async def verify_registration(qr_raw: str) -> dict:
    """
    Verifies a scanned QR. Handles:
      - New format: {"rid": ..., "uid": ..., "eid": ...}
      - Legacy format: {"id": ..., "name": ..., "email": ..., "event": ...}
    """
    try:
        data = json.loads(qr_raw)
    except (json.JSONDecodeError, TypeError):
        data = {"id": qr_raw}

    # New format
    if "rid" in data:
        reg_id = data["rid"]
        try:
            reg_res = await (
                supabase_admin.table("registrations")
                .select("id, user_qr_code, event_id, attended_at")
                .eq("id", reg_id)
                .single()
                .execute()
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if not reg_res.data:
            raise HTTPException(status_code=404, detail="Registration not found.")

        reg = reg_res.data

        # Fetch user + event in parallel
        user_res, event_res = await asyncio.gather(
            supabase_admin.table("users")
            .select("name, email, avatar_url")
            .eq("qr_code_data", reg["user_qr_code"])
            .single()
            .execute(),
            supabase_admin.table("events")
            .select("id, title")
            .eq("id", reg["event_id"])
            .single()
            .execute(),
        )

        already_marked = bool(reg.get("attended_at"))
        attended_at = reg.get("attended_at")

        if not already_marked:
            attended_at = datetime.now(timezone.utc).isoformat()

            async def _mark():
                try:
                    await (
                        supabase_admin.table("registrations")
                        .update({"attended_at": attended_at})
                        .eq("id", reg_id)
                        .execute()
                    )
                except Exception:
                    pass

            asyncio.create_task(_mark())

        u = user_res.data or {}
        e = event_res.data or {}

        return {
            "valid": True,
            "already_marked": already_marked,
            "format": "registration",
            "user": {
                "id": reg["user_qr_code"],
                "name": u.get("name", "Unknown"),
                "email": u.get("email", ""),
                "avatar_url": u.get("avatar_url"),
                "attended_at": attended_at,
            },
            "event": {"id": reg["event_id"], "title": e.get("title", "—")},
        }

    # Legacy format
    search_id = data.get("id", qr_raw)
    try:
        res = await (
            supabase_admin.table("users")
            .select("qr_code_data, name, email, attended_at")
            .eq("qr_code_data", search_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not res.data:
        raise HTTPException(status_code=404, detail="User not found.")

    user = res.data[0]
    already_marked = bool(user.get("attended_at"))
    attended_at = user.get("attended_at")

    if not already_marked:
        attended_at = datetime.now(timezone.utc).isoformat()

        async def _mark_legacy():
            try:
                await (
                    supabase_admin.table("users")
                    .update({"attended_at": attended_at})
                    .eq("qr_code_data", search_id)
                    .execute()
                )
            except Exception:
                pass

        asyncio.create_task(_mark_legacy())

    return {
        "valid": True,
        "already_marked": already_marked,
        "format": "legacy",
        "user": {
            "id": user["qr_code_data"],
            "name": user["name"],
            "email": user["email"],
            "attended_at": attended_at,
        },
        "event": None,
    }


def _generate_qr_data_url(text: str) -> str:
    """CPU-bound — call via asyncio.to_thread."""
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"
