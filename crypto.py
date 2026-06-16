"""Cifrado at-rest de secretos (contraseñas SMTP) con Fernet.

La clave deriva de la env var `ETIQUETAS_SECRET` (o `SECRET_KEY` como fallback),
que vive FUERA de la base: si se filtra solo el archivo SQLite (un backup, una
copia), las contraseñas no quedan legibles. La app necesita la clave para enviar,
así que esto NO protege contra quien tiene base + entorno a la vez.

`dec()` es tolerante: si el valor no es un token Fernet (texto plano heredado de
una versión vieja), lo devuelve tal cual, así la migración es transparente — al
volver a guardar la contraseña queda cifrada.

OJO: si cambiás `ETIQUETAS_SECRET`/`SECRET_KEY`, los valores ya cifrados dejan de
poder descifrarse → hay que volver a cargar las contraseñas SMTP.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _key():
    src = (os.environ.get("ETIQUETAS_SECRET")
           or os.environ.get("SECRET_KEY")
           or "etiquetas-encomiendas-local")
    return base64.urlsafe_b64encode(hashlib.sha256(src.encode()).digest())


def enc(s):
    """Cifra un string; vacío/None pasa igual (no se cifra '')."""
    if not s:
        return s or ""
    return Fernet(_key()).encrypt(s.encode()).decode()


def dec(s):
    """Descifra; si no es un token válido (texto plano heredado), lo devuelve."""
    if not s:
        return s or ""
    try:
        return Fernet(_key()).decrypt(s.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return s
