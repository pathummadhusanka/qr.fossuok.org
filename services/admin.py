import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from fpdf import FPDF

from config.supabase import supabase_admin


# user management
async def fetch_user_stat():
    try:
        res_reg_task = supabase_admin.table("users").select("id", count="exact").eq("role", "participant").execute()
        # Fetch all attended registrations and count DISTINCT users
        res_att_task = (
            supabase_admin.table("registrations")
            .select("user_qr_code")
            .not_.is_("attended_at", "null")
            .execute()
        )
        res_reg, res_att = await asyncio.gather(res_reg_task, res_att_task)
        total_registered = res_reg.count or 0
        # Distinct users who attended at least one event
        total_attended = len({r["user_qr_code"] for r in (res_att.data or [])})
    except Exception:
        total_registered = 0
        total_attended = 0

    return total_registered, total_attended


async def get_all_participants():
    """
    Full attendance report: all participants with aggregate registration/attendance counts.
    """
    try:
        users_res, reg_res = await asyncio.gather(
            supabase_admin.table("users")
            .select("qr_code_data, name, email, role")
            .eq("role", "participant")
            .order("name")
            .execute(),
            supabase_admin.table("registrations")
            .select("user_qr_code, attended_at")
            .execute(),
        )
        users = users_res.data or []
        registrations = reg_res.data or []
    except Exception:
        return []

    reg_count: dict = defaultdict(int)
    att_count: dict = defaultdict(int)
    for r in registrations:
        reg_count[r["user_qr_code"]] += 1
        if r["attended_at"]:
            att_count[r["user_qr_code"]] += 1

    return [
        {
            **u,
            "events_registered": reg_count[u["qr_code_data"]],
            "events_attended": att_count[u["qr_code_data"]],
            "attended_at": att_count[u["qr_code_data"]] > 0,
        }
        for u in users
    ]


async def get_participants_for_event(event_id: str):
    """
    Per-event attendance report: all registrations for a specific event
    merged with user details.
    Returns (participants_list, event_dict).
    """
    try:
        event_res, reg_res = await asyncio.gather(
            supabase_admin.table("events")
            .select("id, title")
            .eq("id", event_id)
            .single()
            .execute(),
            supabase_admin.table("registrations")
            .select("user_qr_code, registered_at, attended_at")
            .eq("event_id", event_id)
            .order("registered_at")
            .execute(),
        )
    except Exception:
        return [], None

    event = event_res.data
    registrations = reg_res.data or []
    if not registrations:
        return [], event

    user_qr_codes = [r["user_qr_code"] for r in registrations]
    try:
        users_res = await (
            supabase_admin.table("users")
            .select("qr_code_data, name, email, role, participant_type, student_id, university, organization, job_role")
            .in_("qr_code_data", user_qr_codes)
            .execute()
        )
        users_by_qr = {u["qr_code_data"]: u for u in (users_res.data or [])}
    except Exception:
        users_by_qr = {}

    participants = []
    for reg in registrations:
        user = users_by_qr.get(reg["user_qr_code"], {})
        participants.append({**user, "attended_at": reg["attended_at"], "registered_at": reg["registered_at"]})

    return participants, event


async def get_all_users():
    try:
        res = await (
            supabase_admin.table("users")
            .select(
                "github_id, name, email, avatar_url, role, created_at, "
                "participant_type, student_id, university, study_year, "
                "organization, job_role"
            )
            .order("role")
            .order("created_at", desc=True)
            .execute()
        )
        users_list = res.data or []
    except Exception:
        users_list = []

    return users_list


async def change_user_role(github_id: str, role: str = "admin"):
    try:
        await (
            supabase_admin.table("users")
            .update({"role": role})
            .eq("github_id", github_id)
            .execute()
        )
        return None, True
    except Exception as e:
        return e, False


async def delete_user_from_db(github_id: str):
    try:
        # Fetch the user's QR code ID so we can clean up registrations first
        user_res = await (
            supabase_admin.table("users")
            .select("qr_code_data")
            .eq("github_id", github_id)
            .single()
            .execute()
        )
        qr_code_data = user_res.data.get("qr_code_data") if user_res.data else None

        # Delete registrations first to avoid FK constraint violation
        if qr_code_data:
            await (
                supabase_admin.table("registrations")
                .delete()
                .eq("user_qr_code", qr_code_data)
                .execute()
            )

        # Now delete the user
        await (
            supabase_admin.table("users")
            .delete()
            .eq("github_id", github_id)
            .execute()
        )
        return None, True
    except Exception as e:
        return str(e), False


def generate_pdf(participants, event_title: str = "All Events", per_event: bool = False):
    pdf = FPDF()
    pdf.add_page()

    # ── Header ──────────────────────────────────────────────────────────────
    pdf.set_font("Arial", "B", 20)
    pdf.set_text_color(75, 46, 131)
    pdf.cell(0, 15, "Attendance Report", ln=True, align="C")

    pdf.set_font("Arial", "B", 12)
    pdf.set_text_color(75, 46, 131)
    pdf.cell(0, 8, event_title, ln=True, align="C")

    pdf.set_font("Arial", "", 9)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, f"Generated on: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC", ln=True, align="C")
    pdf.ln(8)

    h = 8

    if per_event:
        # ── Per-event: Name | Email | Affiliation | Status ──────────────────
        cols = [("Name", 50), ("Email", 60), ("Affiliation", 50), ("Status", 30)]
        pdf.set_font("Arial", "B", 11)
        pdf.set_fill_color(75, 46, 131)
        pdf.set_text_color(255, 255, 255)
        for col_name, width in cols:
            pdf.cell(width, 10, col_name, border=1, align="C", fill=True)
        pdf.ln()

        pdf.set_font("Arial", "", 9)
        fill = False
        for p in participants:
            pdf.set_fill_color(245, 245, 245)
            name = str(p.get("name", "N/A"))[:28]
            email = str(p.get("email", "N/A"))[:32]

            ptype = p.get("participant_type", "")
            if ptype == "uok_student":
                affil = f"UoK | {p.get('student_id', '')}"
            elif ptype == "other_university":
                affil = str(p.get("university", ""))[:22]
            elif ptype == "industry":
                affil = f"{p.get('organization', '')} | {p.get('job_role', '')}"[:22]
            else:
                affil = "—"

            status = "Present" if p.get("attended_at") else "Absent"
            s_color = (40, 167, 69) if status == "Present" else (220, 53, 69)

            pdf.set_text_color(0, 0, 0)
            pdf.cell(50, h, name, border=1, fill=fill)
            pdf.cell(60, h, email, border=1, fill=fill)
            pdf.cell(50, h, affil, border=1, fill=fill)
            pdf.set_text_color(*s_color)
            pdf.set_font("Arial", "B", 9)
            pdf.cell(30, h, status, border=1, fill=fill, align="C")
            pdf.set_font("Arial", "", 9)
            pdf.set_text_color(0, 0, 0)
            pdf.ln()
            fill = not fill

    else:
        # ── Full report: Name | Email | Registered | Attended ────────────────
        cols = [("Name", 55), ("Email", 70), ("Registered", 35), ("Attended", 30)]
        pdf.set_font("Arial", "B", 11)
        pdf.set_fill_color(75, 46, 131)
        pdf.set_text_color(255, 255, 255)
        for col_name, width in cols:
            pdf.cell(width, 10, col_name, border=1, align="C", fill=True)
        pdf.ln()

        pdf.set_font("Arial", "", 9)
        fill = False
        for p in participants:
            pdf.set_fill_color(245, 245, 245)
            name = str(p.get("name", "N/A"))[:30]
            email = str(p.get("email", "N/A"))[:38]
            registered = str(p.get("events_registered", 0))
            attended = str(p.get("events_attended", 0))

            pdf.set_text_color(0, 0, 0)
            pdf.cell(55, h, name, border=1, fill=fill)
            pdf.cell(70, h, email, border=1, fill=fill)
            pdf.cell(35, h, registered, border=1, fill=fill, align="C")
            a_color = (40, 167, 69) if int(attended) > 0 else (220, 53, 69)
            pdf.set_text_color(*a_color)
            pdf.set_font("Arial", "B", 9)
            pdf.cell(30, h, attended, border=1, fill=fill, align="C")
            pdf.set_font("Arial", "", 9)
            pdf.set_text_color(0, 0, 0)
            pdf.ln()
            fill = not fill

    pdf_output = pdf.output(dest="S")
    return pdf_output
