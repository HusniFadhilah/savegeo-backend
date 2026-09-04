"""SMTP email helpers for transactional user-facing messages."""
from __future__ import annotations

import html
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    pass


def _from_name() -> str:
    settings = get_settings()
    name = settings.mail_from_name.strip()
    if not name or name == "${APP_NAME}":
        return settings.app_name
    return name


def _sender() -> str:
    settings = get_settings()
    address = settings.mail_from_address or settings.mail_username
    if not address:
        raise EmailDeliveryError("MAIL_FROM_ADDRESS atau MAIL_USERNAME belum dikonfigurasi.")
    return f"{_from_name()} <{address}>"


def _smtp_send(message: EmailMessage) -> None:
    settings = get_settings()
    if settings.mail_mailer.lower() != "smtp":
        raise EmailDeliveryError("Hanya MAIL_MAILER=smtp yang didukung saat ini.")
    if not settings.mail_host:
        raise EmailDeliveryError("MAIL_HOST belum dikonfigurasi.")

    try:
        with smtplib.SMTP(settings.mail_host, settings.mail_port, timeout=30) as smtp:
            if settings.mail_encryption.lower() == "tls":
                smtp.starttls()
            if settings.mail_username:
                smtp.login(settings.mail_username, settings.mail_password)
            smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - normalize SMTP/library errors for route handlers
        logger.warning("Email delivery failed: %s", exc)
        raise EmailDeliveryError(str(exc)) from exc


def send_password_reset_email(to_address: str, username: str, reset_url: str) -> None:
    settings = get_settings()
    app_name = html.escape(settings.app_name)
    safe_username = html.escape(username)
    safe_url = html.escape(reset_url, quote=True)
    expiry_minutes = settings.password_reset_token_expire_minutes

    subject = f"Reset Password {settings.app_name}"
    text = (
        f"Halo {username},\n\n"
        f"Kami menerima permintaan reset password untuk akun {settings.app_name} Anda.\n"
        f"Buka link berikut dalam {expiry_minutes} menit:\n{reset_url}\n\n"
        "Jika Anda tidak meminta reset password, abaikan email ini."
    )
    html_body = f"""\
<!doctype html>
<html lang="id">
  <body style="margin:0;background:#eef6f0;font-family:Inter,Segoe UI,Arial,sans-serif;color:#173823;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef6f0;padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;background:#ffffff;border-radius:18px;overflow:hidden;border:1px solid #d9eadf;box-shadow:0 18px 48px rgba(23,56,35,0.16);">
            <tr>
              <td style="padding:30px 30px 24px;background:linear-gradient(135deg,#1b5e20,#2e7d32);color:#ffffff;">
                <div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;opacity:.82;">{app_name}</div>
                <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">Reset password akun Anda</h1>
                <p style="margin:10px 0 0;font-size:15px;line-height:1.6;color:#dff7e5;">Link aman ini hanya berlaku {expiry_minutes} menit.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:30px;">
                <p style="margin:0 0 14px;font-size:16px;">Halo <strong>{safe_username}</strong>,</p>
                <p style="margin:0 0 22px;font-size:15px;line-height:1.7;color:#415947;">
                  Kami menerima permintaan untuk mengganti password akun Anda. Klik tombol di bawah untuk membuat password baru.
                </p>
                <p style="margin:0 0 26px;text-align:center;">
                  <a href="{safe_url}" style="display:inline-block;background:#2e7d32;color:#ffffff;text-decoration:none;font-weight:700;padding:13px 22px;border-radius:999px;box-shadow:0 8px 18px rgba(46,125,50,0.26);">Buat Password Baru</a>
                </p>
                <div style="padding:14px 16px;border-radius:12px;background:#f8faf8;border:1px solid #e2ece4;color:#52665a;font-size:13px;line-height:1.6;">
                  Jika tombol tidak bisa dibuka, salin link ini ke browser:<br>
                  <a href="{safe_url}" style="color:#1b5e20;word-break:break-all;">{safe_url}</a>
                </div>
                <p style="margin:22px 0 0;font-size:13px;line-height:1.6;color:#6b7b70;">
                  Jika Anda tidak meminta reset password, abaikan email ini. Password lama tetap berlaku sampai Anda membuat password baru.
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _sender()
    message["To"] = to_address
    message.set_content(text)
    message.add_alternative(html_body, subtype="html")
    _smtp_send(message)
