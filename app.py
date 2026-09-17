from __future__ import annotations

import base64
import csv
import io
import random
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import date, datetime
from email.header import Header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Dict, List, Tuple

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "template.html"
LOGO_PATH = APP_DIR / "gsslogo.png"
LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
DEFAULT_GAP_MIN = 30
DEFAULT_GAP_MAX = 50
DEFAULT_BATCH_CAP = 50

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class Recipient:
    email: str
    name: str = "Candidate"


@dataclass(frozen=True)
class SenderAccount:
    name: str
    email: str
    password: str


def get_logo_base64() -> str:
    if LOGO_PATH.exists():
        try:
            return base64.b64encode(LOGO_PATH.read_bytes()).decode("utf-8")
        except Exception:
            return ""
    return ""


def load_gmail_accounts() -> Dict[str, SenderAccount]:
    accounts: Dict[str, SenderAccount] = {}
    try:
        raw = st.secrets.get("gmail_accounts", {})
        for account_name, values in raw.items():
            email = str(values.get("email", "")).strip()
            password = str(values.get("password", "")).strip()
            if email and password:
                accounts[str(account_name)] = SenderAccount(
                    name=str(account_name), email=email, password=password
                )
    except Exception:
        return {}
    return accounts


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value.strip()))


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def html_to_text(html: str) -> str:
    # Email-safe plain-text fallback without extra dependencies
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?is)<!--.*?-->", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def render_template(
    template: str,
    *,
    interview_date: date,
    interview_time: str,
    contact_mobile: str,
    contact_email: str,
    company_email: str,
    candidate_name: str = "Candidate",
) -> Tuple[str, str]:
    readable_date = interview_date.strftime("%A, %d %B %Y")
    day_suffix = (
        "th"
        if 11 <= interview_date.day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(interview_date.day % 10, "th")
    )
    display_date = interview_date.strftime(f"%A, {interview_date.day}{day_suffix} %B, %Y")

    replacements = {
        "{{CANDIDATE_NAME}}": "Candidate",
        "{{INTERVIEW_DATE}}": display_date,
        "{{INTERVIEW_TIME}}": escape_html(interview_time),
        "{{INTERVIEW_DATE_ISO}}": interview_date.isoformat(),
        "{{CONTACT_MOBILE}}": escape_html(contact_mobile),
        "{{CONTACT_MOBILE_TEL}}": re.sub(r"[^0-9+]", "", contact_mobile),
        "{{CONTACT_EMAIL}}": escape_html(contact_email),
        "{{COMPANY_EMAIL}}": escape_html(company_email),
        "{{GENERATED_DATE}}": readable_date,
    }

    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    # Legacy static string fallbacks so any un-tokenized copies still update seamlessly
    rendered = rendered.replace("Tuesday, 15th September, 2026", display_date)
    rendered = rendered.replace("+91 74185 78889", contact_mobile)
    rendered = rendered.replace("gateway.feniauxily007@gmail.com", contact_email)

    plain = html_to_text(rendered)
    return rendered, plain


def parse_recipients(raw: str) -> Tuple[List[Recipient], List[str]]:
    """Parse recipients from pasted text. Candidates are automatically treated as 'Candidate'."""
    recipients: List[Recipient] = []
    errors: List[str] = []
    seen = set()

    for idx, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        email = line
        # If input has format "Name <email@domain.com>"
        if "<" in email and ">" in email:
            m = re.search(r"<([^>]+)>", email)
            if m:
                email = m.group(1)
        # If input has comma or semicolon e.g. "Candidate Name, email@domain.com"
        elif "," in email or ";" in email:
            delimiters = [",", ";"]
            for d in delimiters:
                if d in email:
                    parts = email.split(d)
                    for p in reversed(parts):
                        p_clean = p.strip()
                        if is_valid_email(p_clean):
                            email = p_clean
                            break

        email = email.strip().lower()
        if not is_valid_email(email):
            errors.append(f"Line {idx}: invalid email address — '{line}'")
            continue
        if email in seen:
            errors.append(f"Line {idx}: duplicate email skipped — '{email}'")
            continue

        seen.add(email)
        recipients.append(Recipient(email=email, name="Candidate"))

    return recipients, errors


def parse_uploaded_csv(data: bytes) -> Tuple[List[Recipient], List[str]]:
    """Parse recipients from CSV file. Only requires an email column; candidate name proceeds as 'Candidate'."""
    recipients: List[Recipient] = []
    errors: List[str] = []
    try:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return [], ["CSV is empty or missing headers. Please include an 'email' column."]

        fields = {f.strip().lower(): f for f in reader.fieldnames if f}
        email_key = (
            fields.get("email")
            or fields.get("gmail")
            or fields.get("email id")
            or fields.get("email_id")
            or fields.get("email address")
            or fields.get("e-mail")
            or fields.get("mail")
        )
        if not email_key:
            for k, original in fields.items():
                if "email" in k or "gmail" in k or "mail" in k:
                    email_key = original
                    break

        if not email_key:
            return [], ["CSV must contain an 'email' (or 'gmail') column header."]

        seen = set()
        for row_idx, row in enumerate(reader, start=2):
            raw_val = str(row.get(email_key, "")).strip().lower()
            if not raw_val:
                continue
            if not is_valid_email(raw_val):
                errors.append(f"Row {row_idx}: invalid email — '{raw_val}'")
                continue
            if raw_val in seen:
                errors.append(f"Row {row_idx}: duplicate email skipped — '{raw_val}'")
                continue

            seen.add(raw_val)
            recipients.append(Recipient(email=raw_val, name="Candidate"))
    except UnicodeDecodeError:
        return [], ["CSV must be UTF-8 encoded."]
    except Exception as exc:
        return [], [f"Could not read CSV: {exc}"]

    return recipients, errors


def get_app_config() -> dict:
    cfg = {
        "smtp_host": DEFAULT_SMTP_HOST,
        "smtp_port": DEFAULT_SMTP_PORT,
        "gap_min": DEFAULT_GAP_MIN,
        "gap_max": DEFAULT_GAP_MAX,
        "batch_cap": DEFAULT_BATCH_CAP,
        "timezone_note": "Use your local/business timezone for the interview date.",
    }
    try:
        section = st.secrets.get("app", {})
        for key in cfg:
            if key in section:
                cfg[key] = section[key]
    except Exception:
        pass
    return cfg


def smtp_connect(account: SenderAccount, host: str, port: int):
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    server.login(account.email, account.password)
    return server


def build_message(
    *,
    sender: SenderAccount,
    recipient: Recipient,
    subject: str,
    html_body: str,
    plain_body: str,
    contact_email: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender.email
    msg["To"] = recipient.email
    msg["Reply-To"] = contact_email
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.email.split("@", 1)[-1])
    msg["MIME-Version"] = "1.0"
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def write_log(rows: List[dict], started_at: datetime) -> Path:
    path = LOG_DIR / f"email_run_{started_at.strftime('%Y%m%d_%H%M%S')}.csv"
    fields = ["timestamp", "recipient_name", "recipient_email", "sender", "status", "error"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    st.set_page_config(
        page_title="Gateway Interview Mailer",
        page_icon="✉️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    logo_b64 = get_logo_base64()

    st.markdown(
        """
        <style>
        /* Base page styling */
        .stApp {
            background: linear-gradient(180deg, #f8fafc 0%, #edf2f7 100%);
            color: #1e293b;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        /* Sidebar Styling: Sleek corporate navy */
        [data-testid="stSidebar"] {
            background: #0b1320 !important;
            border-right: 1px solid #1e293b;
        }

        .sidebar-brand-card {
            background: #ffffff;
            padding: 12px 14px;
            border-radius: 12px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.35);
            margin-bottom: 14px;
            text-align: center;
        }

        /* Explicit, high-contrast sidebar text */
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,
        [data-testid="stSidebar"] h4 {
            color: #f8fafc !important;
            font-weight: 700 !important;
            margin-top: 12px !important;
            margin-bottom: 8px !important;
        }

        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p {
            color: #e2e8f0 !important;
            font-size: 14px !important;
            font-weight: 500 !important;
        }

        /* Sidebar Selectbox & Inputs: High contrast dark styling with sharp white text */
        [data-testid="stSidebar"] [data-baseweb="select"] > div {
            background-color: #162235 !important;
            border: 1px solid #334b6b !important;
            border-radius: 8px !important;
        }

        [data-testid="stSidebar"] [data-baseweb="select"] div,
        [data-testid="stSidebar"] [data-baseweb="select"] span {
            color: #ffffff !important;
            font-weight: 600 !important;
        }

        [data-testid="stSidebar"] [data-baseweb="select"] svg {
            fill: #38bdf8 !important;
        }

        [data-testid="stSidebar"] .stTextInput input {
            background-color: #162235 !important;
            color: #ffffff !important;
            border: 1px solid #334b6b !important;
            border-radius: 8px !important;
        }

        [data-testid="stSidebar"] .stTextInput input::placeholder {
            color: #64748b !important;
        }

        /* Dropdown popover list items */
        ul[role="listbox"] {
            background-color: #162235 !important;
            border: 1px solid #334b6b !important;
        }
        ul[role="listbox"] li {
            color: #f8fafc !important;
            background-color: #162235 !important;
        }
        ul[role="listbox"] li:hover,
        ul[role="listbox"] li[aria-selected="true"] {
            background-color: #0284c7 !important;
            color: #ffffff !important;
        }

        /* Sender pill - high contrast modern cyan/navy */
        .sender-pill {
            background: linear-gradient(135deg, #0c2340 0%, #113259 100%);
            border: 1px solid #0284c7;
            padding: 12px 14px;
            border-radius: 10px;
            color: #ffffff !important;
            font-weight: 600;
            font-size: 13.5px;
            margin-top: 12px;
            word-break: break-all;
            box-shadow: 0 4px 14px rgba(2, 132, 199, 0.2);
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .sender-pill span, .sender-pill strong {
            color: #ffffff !important;
        }

        /* Policy Card inside sidebar */
        .policy-card {
            background: #111b2b;
            border: 1px solid #1e2c42;
            border-radius: 10px;
            padding: 14px;
            margin-top: 10px;
        }
        .policy-item {
            font-size: 12.5px;
            color: #cbd5e1;
            margin-bottom: 8px;
            line-height: 1.45;
        }
        .policy-item strong {
            color: #f8fafc;
        }

        /* Hero Banner with Gateway Branding */
        .hero {
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 60%, #0369a1 100%);
            padding: 26px 32px;
            border-radius: 16px;
            color: white;
            margin-bottom: 24px;
            box-shadow: 0 12px 30px rgba(15, 23, 42, 0.16);
        }
        .hero h1 {
            margin: 6px 0 8px 0;
            font-size: 30px;
            font-weight: 800;
            color: #ffffff;
            letter-spacing: -0.5px;
        }
        .hero p {
            margin: 0;
            color: #cbd5e1;
            font-size: 14.5px;
            line-height: 1.5;
        }
        .hero-badge {
            display: inline-block;
            background: rgba(14, 165, 233, 0.22);
            border: 1px solid rgba(14, 165, 233, 0.45);
            color: #38bdf8;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1.2px;
            padding: 4px 10px;
            border-radius: 20px;
            text-transform: uppercase;
        }
        .hero-logo-container {
            background: #ffffff;
            padding: 10px 18px;
            border-radius: 12px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
        }

        /* Section titles */
        .section-title {
            font-size: 17px;
            font-weight: 700;
            color: #0f172a;
            margin: 16px 0 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .section-title::before {
            content: "";
            display: inline-block;
            width: 4px;
            height: 18px;
            background: #0284c7;
            border-radius: 2px;
        }

        /* Disabled input readability */
        .stTextInput input:disabled {
            color: #0f172a !important;
            -webkit-text-fill-color: #0f172a !important;
            background-color: #e2e8f0 !important;
            border: 1px solid #cbd5e1 !important;
            font-weight: 600 !important;
            opacity: 1 !important;
        }

        /* Metric Cards */
        [data-testid="stMetric"] {
            background: #ffffff;
            padding: 14px 18px;
            border-radius: 12px;
            border: 1px solid #e2e8f0;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.03);
        }
        [data-testid="stMetricLabel"] {
            color: #64748b !important;
            font-weight: 600 !important;
        }
        [data-testid="stMetricValue"] {
            color: #0f172a !important;
            font-weight: 800 !important;
        }

        /* Status & info cards */
        .status-card {
            padding: 16px 20px;
            border-radius: 12px;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.04);
            color: #0f172a;
            font-size: 14px;
        }
        .small-note {
            color: #64748b;
            font-size: 12.5px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    accounts = load_gmail_accounts()
    config = get_app_config()

    # Hero Banner with Gateway Logo Branding
    logo_header_html = (
        f'<div class="hero-logo-container"><img src="data:image/png;base64,{logo_b64}" alt="Gateway Software Solutions" style="max-height: 52px; max-width: 100%; display: block;" /></div>'
        if logo_b64
        else ""
    )
    st.markdown(
        f"""
        <div class="hero">
          <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 20px;">
            <div style="flex: 1; min-width: 280px;">
              <div class="hero-badge">GATEWAY HR AUTOMATION</div>
              <h1>Gateway Interview Mailer</h1>
              <p>Corporate interview scheduling automation with paced delivery, real-time logging, and verified Gateway branding.</p>
            </div>
            {logo_header_html}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        # Corporate Logo in Sidebar
        if logo_b64:
            st.markdown(
                f"""
                <div class="sidebar-brand-card">
                    <img src="data:image/png;base64,{logo_b64}" alt="Gateway Software Solutions" style="width: 100%; max-width: 220px; height: auto; display: block; margin: 0 auto;" />
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown("### 🏢 Gateway Software Solutions")

        st.markdown("### Sending account")
        if accounts:
            labels = list(accounts.keys()) + ["✍️ Manual Gmail + App Password"]
            selected_label = st.selectbox("Which Gmail ID should send these emails?", labels)
            manual_mode = selected_label.startswith("✍️")
            if manual_mode:
                manual_email = st.text_input("Gmail ID", placeholder="youraccount@gmail.com")
                manual_password = st.text_input(
                    "Gmail App Password",
                    type="password",
                    placeholder="16-character app password",
                )
                sender = SenderAccount("Manual", manual_email.strip(), manual_password.strip())
            else:
                sender = accounts[selected_label]
        else:
            st.warning("No configured sender accounts were found in secrets.toml.")
            st.info("You can use the manual account option below:")
            manual_email = st.text_input("Gmail ID", placeholder="youraccount@gmail.com")
            manual_password = st.text_input("Gmail App Password", type="password")
            sender = SenderAccount("Manual", manual_email.strip(), manual_password.strip())

        if sender.email:
            st.markdown(
                f"""
                <div class="sender-pill">
                    <span>✉ Using:</span>
                    <strong>{escape_html(sender.email)}</strong>
                </div>
                """,
                unsafe_allow_html=True,
            )

        st.markdown("---")
        st.markdown("### Delivery policy")
        st.markdown(
            f"""
            <div class="policy-card">
                <div class="policy-item"><strong>SMTP Host:</strong> {config['smtp_host']}:{config['smtp_port']}</div>
                <div class="policy-item"><strong>Pacing:</strong> {config['gap_min']}–{config['gap_max']} seconds interval</div>
                <div class="policy-item"><strong>Safety Batch Cap:</strong> {config['batch_cap']} recipients/run</div>
                <div class="policy-item" style="margin-bottom:0;">One selected sender account per run. Strictly adheres to Gmail sending limits.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    col1, col2 = st.columns(2)
    with col1:
        st.markdown('<div class="section-title">Interview details</div>', unsafe_allow_html=True)
        contact_mobile = st.text_input("Contact mobile number", value="+91 74185 78889")
        contact_email = st.text_input("Contact Gmail ID", value=sender.email if sender.email else "")
        sub_c1, sub_c2 = st.columns(2)
        with sub_c1:
            interview_date = st.date_input("Date of Interview", value=date.today())
        with sub_c2:
            interview_time = st.text_input("Interview Time Slot", value="10:00 AM to 12:00 PM")

    with col2:
        st.markdown('<div class="section-title">Subject line</div>', unsafe_allow_html=True)
        standard_subject = st.text_input(
            "Email subject",
            value="Internship Interview Invitation | You've been shortlisted - Walk-in Interview",
            help="The subject line candidates will receive in their email inbox.",
        ).strip()
        if not standard_subject:
            standard_subject = "Internship Interview Invitation | You've been shortlisted - Walk-in Interview"
        st.caption("Official subject line sent to all candidates.")

    st.markdown('<div class="section-title">Recipients</div>', unsafe_allow_html=True)
    input_mode = st.radio("How will you provide recipient emails?", ["Paste list", "Upload CSV"], horizontal=True)
    recipients: List[Recipient] = []
    recipient_errors: List[str] = []

    if input_mode == "Paste list":
        raw_recipients = st.text_area(
            "Paste recipient Gmail / Email IDs (one per line)",
            height=180,
            placeholder="candidate1@gmail.com\ncandidate2@gmail.com\n\n# Lines beginning with # are ignored",
            help="Enter email addresses, one per line. Candidate names are not required and will automatically be addressed as 'Candidate'.",
        )
        recipients, recipient_errors = parse_recipients(raw_recipients)
    else:
        uploaded = st.file_uploader(
            "Upload recipient CSV",
            type=["csv"],
            help="Only an 'email' (or 'gmail') column is needed. Names are not required.",
        )
        if uploaded is not None:
            recipients, recipient_errors = parse_uploaded_csv(uploaded.getvalue())
            st.caption("CSV data is processed in-memory for this run only.")

    if recipient_errors:
        with st.expander(f"⚠️ Validation notices ({len(recipient_errors)})", expanded=False):
            for error in recipient_errors:
                st.write(f"• {error}")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Valid recipients", len(recipients))
    k2.metric("Skipped/invalid", len(recipient_errors))
    k3.metric("Pacing interval", f"{config['gap_min']}–{config['gap_max']}s")
    k4.metric("Safety batch cap", int(config["batch_cap"]))

    if recipients:
        st.dataframe(
            [{"#": i, "Recipient Email": r.email, "Candidate": "Candidate"} for i, r in enumerate(recipients, start=1)],
            use_container_width=True,
            hide_index=True,
        )

    if not TEMPLATE_PATH.exists():
        st.error(f"Required template file not found: {TEMPLATE_PATH.name}")
        st.stop()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    preview_html, preview_text = render_template(
        template,
        candidate_name="Candidate",
        interview_date=interview_date,
        interview_time=interview_time,
        contact_mobile=contact_mobile,
        contact_email=contact_email,
        company_email=sender.email,
    )

    with st.expander("👁️ Preview Email (Candidate View)", expanded=False):
        st.markdown(
            f"**From:** `{sender.email or '—'}`  \n"
            f"**To:** `{recipients[0].email if recipients else 'candidate@example.com'}`  \n"
            f"**Subject:** `{standard_subject}`"
        )
        st.components.v1.html(preview_html, height=760, scrolling=True)
        st.download_button(
            "Download rendered HTML preview",
            preview_html,
            file_name="preview.html",
            mime="text/html",
        )

    st.markdown('<div class="section-title">Pre-send checklist</div>', unsafe_allow_html=True)
    checks = {
        "Valid sender Gmail ID": is_valid_email(sender.email),
        "App password supplied": bool(sender.password),
        "Contact Gmail ID valid": is_valid_email(contact_email),
        "Contact mobile supplied": bool(contact_mobile.strip()),
        "Interview date selected": isinstance(interview_date, date),
        "Interview time slot set": bool(interview_time.strip()),
        "Template loaded": bool(template.strip()),
        "At least one recipient": bool(recipients),
    }
    checklist_cols = st.columns(2)
    for i, (label, ok) in enumerate(checks.items()):
        checklist_cols[i % 2].write(f"{'✅' if ok else '❌'} {label}")

    ready = all(checks.values())
    over_cap = len(recipients) > int(config["batch_cap"])
    if over_cap:
        st.warning(
            f"This run contains {len(recipients)} recipients. The configured safety cap is {config['batch_cap']}. "
            "Please split your recipients or adjust batch_cap in configuration."
        )

    st.info(
        "Deliverability notice: Keep complaint/bounce rates low by sending to verified candidate lists only. "
        "The app delivers emails at randomized delays to ensure reliable mailbox delivery."
    )

    send_col, test_col = st.columns([2, 1])
    send_clicked = send_col.button(
        "🚀 Start interview email run",
        type="primary",
        use_container_width=True,
        disabled=not ready or over_cap,
    )
    test_clicked = test_col.button(
        "🔌 Test SMTP login",
        use_container_width=True,
        disabled=not (is_valid_email(sender.email) and bool(sender.password)),
    )

    if test_clicked:
        with st.spinner("Testing SMTP authentication…"):
            try:
                server = smtp_connect(sender, str(config["smtp_host"]), int(config["smtp_port"]))
                server.quit()
                st.success(f"SMTP authentication succeeded for {sender.email}.")
            except Exception as exc:
                st.error(f"SMTP authentication failed: {exc}")

    if send_clicked:
        started_at = datetime.now()
        log_rows: List[dict] = []
        progress = st.progress(0, text="Preparing email run…")
        status_box = st.empty()
        countdown_box = st.empty()
        metrics = st.columns(4)
        sent_count = 0
        failed_count = 0

        status_box.markdown('<div class="status-card">Connecting securely to Gmail SMTP…</div>', unsafe_allow_html=True)
        server = None
        try:
            server = smtp_connect(sender, str(config["smtp_host"]), int(config["smtp_port"]))
            for idx, recipient in enumerate(recipients, start=1):
                progress.progress(
                    (idx - 1) / len(recipients),
                    text=f"Processing {idx}/{len(recipients)} — {recipient.email}",
                )
                status_box.markdown(
                    f'<div class="status-card"><strong>Currently processing:</strong> {escape_html(recipient.email)}<br>'
                    f'<span class="small-note">Recipient: Candidate · Sender: {escape_html(sender.email)}</span></div>',
                    unsafe_allow_html=True,
                )
                try:
                    html_body, text_body = render_template(
                        template,
                        candidate_name="Candidate",
                        interview_date=interview_date,
                        interview_time=interview_time,
                        contact_mobile=contact_mobile,
                        contact_email=contact_email,
                        company_email=sender.email,
                    )
                    message = build_message(
                        sender=sender,
                        recipient=recipient,
                        subject=standard_subject,
                        html_body=html_body,
                        plain_body=text_body,
                        contact_email=contact_email,
                    )
                    server.send_message(message)
                    sent_count += 1
                    log_rows.append({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "recipient_name": "Candidate",
                        "recipient_email": recipient.email,
                        "sender": sender.email,
                        "status": "sent",
                        "error": "",
                    })
                    metrics[0].metric("Sent", sent_count)
                    metrics[1].metric("Failed", failed_count)
                    metrics[2].metric("Remaining", len(recipients) - idx)
                    metrics[3].metric("Progress", f"{idx}/{len(recipients)}")

                    if idx < len(recipients):
                        gap = random.randint(int(config["gap_min"]), int(config["gap_max"]))
                        for remaining in range(gap, 0, -1):
                            countdown_box.info(
                                f"Next email in **{remaining}s** · randomized pacing interval to maintain domain reputation"
                            )
                            time.sleep(1)

                except Exception as exc:
                    failed_count += 1
                    log_rows.append({
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "recipient_name": "Candidate",
                        "recipient_email": recipient.email,
                        "sender": sender.email,
                        "status": "failed",
                        "error": str(exc),
                    })
                    status_box.error(f"Failed sending to {recipient.email}: {exc}")

            progress.progress(1.0, text="Email run completed")
            countdown_box.empty()
            status_box.markdown(
                f'<div class="status-card"><strong>Run completed!</strong> Sent: {sent_count} · Failed: {failed_count}</div>',
                unsafe_allow_html=True,
            )
            log_path = write_log(log_rows, started_at)
            st.success(f"Run complete. Detailed execution log saved to {log_path.name}.")
            st.download_button(
                "📥 Download CSV run log",
                log_path.read_bytes(),
                file_name=log_path.name,
                mime="text/csv",
            )
        except Exception as exc:
            st.error(f"Could not complete the run: {exc}")
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
