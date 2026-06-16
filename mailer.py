"""Envío de emails por SMTP, configurado desde settings (DB).

Best-effort: las funciones devuelven (ok, mensaje) y nunca lanzan hacia el caller,
para que un fallo de correo no rompa el flujo de impresión/despacho.
"""
import smtplib
import ssl
from email.message import EmailMessage

import db


def smtp_configurado():
    return db.get_bool("smtp_enabled") and bool(db.get_setting("smtp_host"))


def _remitente():
    return (db.get_setting("smtp_from") or db.get_setting("smtp_user") or "").strip()


def enviar(destino, asunto, cuerpo, adjunto_path=None, adjunto_nombre=None):
    """Envía un email de texto plano con adjunto opcional. (ok, mensaje)."""
    destino = (destino or "").strip()
    if not destino:
        return False, "sin destinatario"
    if not db.get_setting("smtp_host"):
        return False, "SMTP no configurado"

    msg = EmailMessage()
    msg["From"] = _remitente() or db.get_setting("smtp_user")
    msg["To"] = destino
    msg["Subject"] = asunto
    msg.set_content(cuerpo)

    if adjunto_path:
        try:
            with open(adjunto_path, "rb") as f:
                datos = f.read()
            msg.add_attachment(datos, maintype="image", subtype="jpeg",
                               filename=adjunto_nombre or "ticket.jpg")
        except OSError:
            pass  # si la foto no se puede leer, se manda el correo sin adjunto

    host = db.get_setting("smtp_host")
    port = int(db.get_setting("smtp_port") or 587)
    user = db.get_setting("smtp_user")
    pw = db.get_setting("smtp_password")
    seguridad = (db.get_setting("smtp_security") or "starttls").lower()

    try:
        if seguridad == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ctx) as s:
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                if seguridad == "starttls":
                    s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, pw)
                s.send_message(msg)
        return True, "ok"
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return False, str(exc)
