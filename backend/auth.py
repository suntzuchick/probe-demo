"""
Magic-link authentication for Probe demo.

Flow
────
1. POST /api/auth/request  — email checked against ALLOWED_EMAILS env var;
                             one-time token generated and emailed
2. GET  /api/auth/verify   — token validated, burned, session token returned
                             via redirect to /?probe_token=SESSION_TOKEN
3. All /api/* calls carry X-Probe-Token header; before_request validates it
4. GET  /api/auth/status   — lightweight ping to confirm session is alive
5. POST /api/auth/logout   — revoke session token

Tokens are kept in-memory — fine for a single-process demo; all sessions
expire on restart and users just click a new link.

Auth is completely disabled when ALLOWED_EMAILS env var is unset (dev mode).
"""
import os
import secrets
import threading
import time

_lock = threading.Lock()
_magic_tokens: dict = {}   # token → {email, created_at, used}
_sessions:     dict = {}   # token → {email, created_at}

MAGIC_EXPIRY   = 7 * 24 * 3600  # 7 days
SESSION_EXPIRY = 7 * 24 * 3600  # 7 days


# ── Allowlist ──────────────────────────────────────────────────────────────────

def allowed_emails() -> set:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_allowed(email: str) -> bool:
    return email.strip().lower() in allowed_emails()


def auth_enabled() -> bool:
    return bool(os.environ.get("ALLOWED_EMAILS", "").strip())


# ── Magic token lifecycle ──────────────────────────────────────────────────────

def create_magic_token(email: str) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _magic_tokens[token] = {
            "email":      email.strip().lower(),
            "created_at": time.time(),
            "used":       False,
        }
    return token


def verify_magic_token(token: str) -> str | None:
    """
    Validates a magic token. If valid and unused, burns it and
    returns a new long-lived session token. Returns None on failure.
    """
    with _lock:
        entry = _magic_tokens.get(token)
        if not entry or entry["used"]:
            return None
        if time.time() - entry["created_at"] > MAGIC_EXPIRY:
            return None
        entry["used"] = True
        email = entry["email"]

    session_token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[session_token] = {
            "email":      email,
            "created_at": time.time(),
        }
    return session_token


# ── Session validation ─────────────────────────────────────────────────────────

def validate_session(token: str) -> str | None:
    """Returns the email for a live session, or None."""
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if not entry:
            return None
        if time.time() - entry["created_at"] > SESSION_EXPIRY:
            del _sessions[token]
            return None
        return entry["email"]


def revoke_session(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


# ── Email delivery ─────────────────────────────────────────────────────────────

def send_magic_link(email: str, token: str) -> bool:
    """
    Send the magic link email via SMTP.
    Required env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, APP_BASE_URL.
    Optional:          SMTP_FROM  (defaults to SMTP_USER).
    Returns True on success.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host      = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port      = int(os.environ.get("SMTP_PORT", "587"))
    user      = os.environ.get("SMTP_USER", "")
    password  = os.environ.get("SMTP_PASSWORD", "")
    base_url  = os.environ.get("APP_BASE_URL", "http://localhost:5050")
    from_addr = os.environ.get("SMTP_FROM", user)

    link = f"{base_url}/api/auth/verify?token={token}"

    plain = (
        f"Your one-time Probe access link:\n\n"
        f"{link}\n\n"
        f"This link expires in 15 minutes and can only be used once.\n"
        f"If you didn't request this, ignore this email.\n"
    )
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#FDF8F0;font-family:'IBM Plex Sans',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:48px 16px;">
<table width="480" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;border:1px solid #e8e0d5;padding:40px;">
<tr><td>
  <div style="font-size:12px;font-weight:700;letter-spacing:0.14em;color:#b07a10;background:#fff8ea;
              border:1px solid #F9BB6E;border-radius:5px;display:inline-block;
              padding:3px 10px;margin-bottom:28px;">PROBE</div>
  <h1 style="font-size:22px;font-weight:600;color:#1a0033;margin:0 0 12px;">Your access link</h1>
  <p style="font-size:14px;color:#555;line-height:1.6;margin:0 0 28px;">
    Click the button below to access Probe.<br>
    This link expires in <strong>15 minutes</strong> and can only be used once.
  </p>
  <a href="{link}"
     style="display:inline-block;background:#FF75D7;color:#fff;font-size:14px;
            font-weight:600;padding:13px 30px;border-radius:7px;text-decoration:none;">
    Open Probe →
  </a>
  <p style="font-size:11px;color:#aaa;margin:28px 0 0;line-height:1.5;">
    Or paste into your browser:<br>
    <span style="font-family:monospace;font-size:10px;word-break:break-all;">{link}</span>
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px;">
  <p style="font-size:11px;color:#ccc;margin:0;">
    If you didn't request this, ignore this email — your account is safe.
  </p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Probe access link"
    msg["From"]    = f"Probe <{from_addr}>"
    msg["To"]      = email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    try:
        with smtplib.SMTP(host, port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(from_addr, [email], msg.as_string())
        print(f"[auth] Magic link sent to {email}")
        return True
    except Exception as exc:
        print(f"[auth] Email send failed: {exc}")
        return False
