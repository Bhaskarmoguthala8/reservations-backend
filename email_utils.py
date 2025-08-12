import os
from datetime import date as Date, time as Time, datetime, timedelta
from urllib.parse import urlencode, quote_plus
import resend
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
FROM_EMAIL     = os.getenv("FROM_EMAIL")  # e.g. "The Rambling House <noreply@yourdomain.ie>"
_admin_env     = os.getenv("ADMIN_EMAILS") or os.getenv("ADMIN_EMAIL") or ""
ADMIN_RECIPIENTS = [e.strip() for e in _admin_env.split(",") if e.strip()]

if not RESEND_API_KEY or not FROM_EMAIL:
    raise RuntimeError("RESEND_API_KEY and FROM_EMAIL must be set in .env")

# Debug: confirm who will receive admin emails
print("ADMIN_RECIPIENTS:", ADMIN_RECIPIENTS)

resend.api_key = RESEND_API_KEY

# ---------- Brand / Styles ----------
BRAND_NAME = "The Rambling House Bar & Restaurant"
ACCENT     = "#0e7a4a"   # deep green
ACCENT_LT  = "#eaf6f0"   # light green panel
TEXT       = "#1a1a1a"
MUTED      = "#667085"
BORDER     = "#e6e6e6"
BG         = "#ffffff"

ADDRESS_BLOCK = (
    f"<strong>{BRAND_NAME}</strong><br/>"
    "Main street Laghy, Co. Donegal F94Y048<br/>"
    "Tel: +353 0749740813 &nbsp;&nbsp; Mob: +353 0852533832"
)

def _preheader(text: str) -> str:
    # Hidden preview line many email clients show next to the subject
    return f"<span style='display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden;'>{text}</span>"

def _fmt_date(v) -> str:
    if not v:
        return ""
    if isinstance(v, Date):
        return v.strftime("%A, %d %B %Y")
    try:
        return Date.fromisoformat(str(v)).strftime("%A, %d %B %Y")
    except Exception:
        return str(v)

def _fmt_time(v) -> str:
    if not v:
        return ""
    if isinstance(v, Time):
        return v.strftime("%I:%M %p").lstrip("0")
    s = str(v)
    parts = s.split(":")
    try:
        h = int(parts[0]); m = int(parts[1]) if len(parts) > 1 else 0
        suf = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suf}"
    except Exception:
        return s

def _s(v) -> str:
    return "" if v is None else str(v)

def _ref(res_id: str | None) -> str:
    if not res_id:
        return "—"
    return res_id.replace("-", "")[-6:].upper()

def _start_end_strings(r: dict, duration_minutes: int = 120) -> tuple[str, str]:
    """Return Google Calendar local time strings like YYYYMMDDTHHMMSS/YYYYMMDDTHHMMSS (no timezone)."""
    d = _s(r.get("date"))
    t = _s(r.get("time"))
    try:
        # handle "HH:MM:SS" or "HH:MM"
        hh, mm = t.split(":")[:2]
        dt = datetime.fromisoformat(f"{d}T{hh}:{mm}:00")
        end = dt + timedelta(minutes=duration_minutes)
        return (dt.strftime("%Y%m%dT%H%M%S"), end.strftime("%Y%m%dT%H%M%S"))
    except Exception:
        # Fallback to all-day
        try:
            dd = Date.fromisoformat(_s(r.get("date"))).strftime("%Y%m%d")
            return (dd, dd)
        except Exception:
            # give empty; Google will let user fill
            return ("", "")

def _gcal_link(r: dict) -> str:
    start, end = _start_end_strings(r)
    params = {
        "action": "TEMPLATE",
        "text": f"Reservation — {BRAND_NAME}",
        "details": (
            "Your booking at The Rambling House.\n\n"
            f"Name: {_s(r.get('name'))}\n"
            f"Guests: {_s(r.get('guests'))}\n"
            f"Reference: {_ref(r.get('id'))}\n"
            "If your plans change, please let us know."
        ),
        "location": "The Rambling House, Main street Laghy, Co. Donegal F94Y048",
    }
    if start and end:
        params["dates"] = f"{start}/{end}"
    return "https://calendar.google.com/calendar/render?" + urlencode(params, quote_via=quote_plus)

def _maps_link() -> str:
    q = "The Rambling House Laghy F94Y048"
    return "https://www.google.com/maps/search/?" + urlencode({"api": "1", "query": q}, quote_via=quote_plus)

def _button(href: str, label: str, bg=ACCENT, color="#ffffff"):
    return (
        f"<a href='{href}' "
        f"style='display:inline-block;padding:10px 14px;background:{bg};color:{color};"
        "text-decoration:none;border-radius:8px;font-weight:600'>"
        f"{label}</a>"
    )

def _details_table(r: dict) -> str:
    rows = [
        ("Reservation name",   _s(r.get("name"))),
        ("Guests",             _s(r.get("guests"))),
        ("Date",               _fmt_date(r.get("date"))),
        ("Time",               _fmt_time(r.get("time"))),
        ("Contact email",      _s(r.get("email"))),
        ("Phone",              _s(r.get("phone"))),
        ("Occasion",           _s(r.get("occasion")) or "—"),
        ("Special requests",   _s(r.get("special_requests")) or "—"),
        ("Reference",          _ref(r.get("id"))),
        ("Status",             _s(r.get("status")) or "—"),
    ]
    trs = "".join(
        f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid {BORDER};color:{MUTED};width:180px;font-weight:600;">{label}</td>
          <td style="padding:10px 12px;border-bottom:1px solid {BORDER};color:{TEXT};">{value}</td>
        </tr>
        """ for label, value in rows
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border:1px solid {BORDER};border-radius:12px;overflow:hidden;background:{BG}">
      <tbody>{trs}</tbody>
    </table>
    """

def _wrapper_html(title: str, preheader: str, intro_html: str, details_html: str, badge: str = "", extra_footer: str = "") -> str:
    return f"""
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#f7f7f7;">
    {_preheader(preheader)}
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f7f7f7;">
      <tr>
        <td style="padding:32px 16px;">
          <table role="presentation" cellpadding="0" cellspacing="0" width="640" align="center" style="margin:0 auto;background:{BG};border-radius:14px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">
            <tr>
              <td style="padding:20px 24px;background:{ACCENT};color:#fff;font-family:Arial, Helvetica, sans-serif;">
                <div style="font-size:13px;opacity:.9;letter-spacing:.08em;text-transform:uppercase;">{BRAND_NAME}</div>
                <div style="font-size:22px;font-weight:700;margin-top:4px;">{title}</div>
                {f'<div style="margin-top:8px;display:inline-block;background:rgba(255,255,255,.18);padding:6px 10px;border-radius:999px;font-size:12px;">{badge}</div>' if badge else ''}
              </td>
            </tr>
            <tr>
              <td style="padding:24px;font-family:Arial, Helvetica, sans-serif;color:{TEXT};">
                {intro_html}
                <div style="height:16px;"></div>
                {details_html}
                <div style="height:16px;"></div>
                {extra_footer}
              </td>
            </tr>
            <tr>
              <td style="padding:24px;color:{MUTED};font-size:14px;line-height:1.5;border-top:1px solid {BORDER}">
                {ADDRESS_BLOCK}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
    """

def _send(to_emails, subject: str, html: str, text_fallback: str = ""):
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    params = {
        "from": FROM_EMAIL,
        "to": to_emails,
        "subject": subject,
        "html": html,
        "text": text_fallback or "Reservation update from The Rambling House.",
    }
    resp = resend.Emails.send(params)
    print(f"Resend -> {to_emails}: {resp}")
    return resp

# ---------- Public: called by main.py ----------

def send_reservation_received(reservation: dict) -> None:
    """On create (pending): send to customer + admins."""
    details = _details_table(reservation)
    gcal = _button(_gcal_link(reservation), "Add to Calendar")
    maps = _button(_maps_link(), "Get Directions", bg="#0a5a36")

    # Customer
    ref = _ref(reservation.get("id"))
    intro_c = (
        f"<p style='margin:0 0 10px 0;'>Hi <strong>{_s(reservation.get('name'))}</strong>, thanks for booking with us.</p>"
        f"<p style='margin:0;'>We’ve received your request and it’s now <strong>pending review</strong> by our team. "
        f"You’ll get another email once we confirm availability.</p>"
        f"<p style='margin:12px 0 0 0;color:{MUTED}'>What happens next?</p>"
        f"<ul style='margin:8px 0 0 20px;padding:0;color:{MUTED}'>"
        "<li>We’ll check tables and confirm ASAP.</li>"
        "<li>If anything changes, just reply to this email or call us.</li>"
        "<li>Please arrive a few minutes early so we can seat you comfortably.</li>"
        "</ul>"
    )
    footer_c = (
        f"<div style='padding:14px 16px;background:{ACCENT_LT};border:1px solid {BORDER};border-radius:10px;'>"
        f"{gcal}&nbsp;&nbsp;{maps}"
        "</div>"
    )
    html_c = _wrapper_html(
        title=f"We’ve got your reservation (Ref: {ref})",
        preheader="Thanks! Your request is pending. We’ll confirm shortly.",
        intro_html=intro_c,
        details_html=details,
        badge="Pending ⏳",
        extra_footer=footer_c,
    )
    _send(reservation["email"], f"📝 Reservation received — Ref {ref}", html_c)

    # Admin(s)
    if ADMIN_RECIPIENTS:
        intro_a = (
            "<p style='margin:0;'>New reservation submitted — please review and confirm if available.</p>"
            f"<p style='margin:8px 0 0 0;color:{MUTED}'>Tip: reply to this email to contact the guest directly.</p>"
        )
        html_a = _wrapper_html(
            title="New reservation (Pending)",
            preheader="A new booking needs review",
            intro_html=intro_a,
            details_html=details,
            badge="Pending ⏳",
            extra_footer="",
        )
        _send(
            ADMIN_RECIPIENTS,
            f"🆕 Pending booking — { _fmt_date(reservation.get('date')) } { _fmt_time(reservation.get('time')) } · {_s(reservation.get('name'))} · {_s(reservation.get('guests'))}p",
            html_a,
        )

def send_status_change(reservation: dict) -> None:
    """On status update: send to customer + admins."""
    status = (_s(reservation.get("status")) or "").lower()
    details = _details_table(reservation)
    gcal = _button(_gcal_link(reservation), "Add to Calendar")
    maps = _button(_maps_link(), "Get Directions", bg="#0a5a36")
    ref = _ref(reservation.get("id"))

    if status == "confirmed":
        subject_c = f"✅ Confirmed — { _fmt_date(reservation.get('date')) } { _fmt_time(reservation.get('time')) } · Ref {ref}"
        badge = "Confirmed ✅"
        intro = (
            f"<p style='margin:0 0 10px 0;'>Hi <strong>{_s(reservation.get('name'))}</strong>, great news!</p>"
            "<p style='margin:0;'>Your reservation is <strong>confirmed</strong>. We can’t wait to welcome you.</p>"
            f"<p style='margin:12px 0 0 0;color:{MUTED}'>Good to know:</p>"
            f"<ul style='margin:8px 0 0 20px;padding:0;color:{MUTED}'>"
            "<li>If you’re running late, just give us a call.</li>"
            "<li>Need to adjust your party size or time? Reply to this email.</li>"
            "</ul>"
        )
    elif status == "cancelled":
        subject_c = f"❌ Cancelled — Ref {ref}"
        badge = "Cancelled ❌"
        intro = (
            f"<p style='margin:0 0 10px 0;'>Hi <strong>{_s(reservation.get('name'))}</strong>,</p>"
            "<p style='margin:0;'>Your reservation has been <strong>cancelled</strong>. "
            "If this was a mistake or you need a new time, reply and we’ll help.</p>"
        )
    else:
        subject_c = f"ℹ️ Update — Status: {status} · Ref {ref}"
        badge = f"Status: {status}"
        intro = (
            f"<p style='margin:0 0 10px 0;'>Hi <strong>{_s(reservation.get('name'))}</strong>,</p>"
            f"<p style='margin:0;'>Your reservation status is now <strong>{status}</strong>.</p>"
        )

    footer_cta = (
        f"<div style='padding:14px 16px;background:{ACCENT_LT};border:1px solid {BORDER};border-radius:10px;'>"
        f"{gcal}&nbsp;&nbsp;{maps}"
        "</div>"
    )
    html_c = _wrapper_html(
        title="Reservation update",
        preheader="Your booking status has changed.",
        intro_html=intro,
        details_html=details,
        badge=badge,
        extra_footer=footer_cta,
    )
    _send(reservation["email"], subject_c, html_c)

    # Admin(s)
    if ADMIN_RECIPIENTS:
        subj_a = f"📣 {status.capitalize()} — { _fmt_date(reservation.get('date')) } { _fmt_time(reservation.get('time')) } · {_s(reservation.get('name'))} · {_s(reservation.get('guests'))}p · Ref {ref}"
        intro_a = "<p style='margin:0;'>Reservation status changed:</p>"
        html_a = _wrapper_html(
            title=f"Reservation {status.capitalize()}",
            preheader="A booking status was updated",
            intro_html=intro_a,
            details_html=details,
            badge=badge,
            extra_footer="",
        )
        _send(ADMIN_RECIPIENTS, subj_a, html_a)
