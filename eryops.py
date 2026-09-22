"""Importar clientes desde una instancia de Eryops (certificados.<dominio>).

Eryops expone `GET /api/v1/clientes` detrás de un JWT que se saca con
`POST /api/v1/auth/login`. Alcanza con un usuario de rol LECTOR (solo lectura).

ponytail: urllib, no requests/httpx — son dos llamadas HTTP, no vale una
dependencia nueva. Sin cache de token: se importa a mano, no en loop.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT = 8
# ponytail: el endpoint de Eryops topea en 500 por página y acá no se pagina.
# Si algún criadero pasa de 500 clientes, el import avisa que quedó truncado y
# recién ahí vale la pena paginar.
LIMITE = 500


class ErrorEryops(Exception):
    pass


class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    """Corta las redirecciones: urllib re-manda el `Authorization` al destino
    nuevo, así que un 302 (o una URL mal configurada) regalaría el token a otro
    host. Si Eryops redirige, que se arregle la URL en /admin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ErrorEryops(f"el servidor redirige a {newurl}; corregí la URL")


_abridor = urllib.request.build_opener(_SinRedirecciones)


def _pedir(url, datos=None, token=None):
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    req = urllib.request.Request(url, data=cuerpo, method="POST" if cuerpo else "GET")
    if cuerpo:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with _abridor.open(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except ErrorEryops:
        raise
    except urllib.error.HTTPError as e:
        detalle = ""
        try:
            detalle = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            pass
        raise ErrorEryops(f"HTTP {e.code}{': ' + str(detalle) if detalle else ''}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ErrorEryops(str(e)) from e


def traer_clientes(base_url, usuario, clave, limite=LIMITE):
    """Devuelve la lista de clientes activos. Lanza ErrorEryops si algo falla."""
    base = (base_url or "").strip().rstrip("/")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ErrorEryops("URL inválida")
    tok = _pedir(f"{base}/api/v1/auth/login", {"usuario": usuario, "clave": clave})
    # Si la cuenta tiene 2FA el login no devuelve access_token: no sirve para un
    # usuario de servicio y hay que decirlo claro en vez de romper con KeyError.
    if "access_token" not in tok:
        raise ErrorEryops("la cuenta pide 2FA")
    datos = _pedir(f"{base}/api/v1/clientes?limite={int(limite)}", token=tok["access_token"])
    return [c for c in datos if c.get("activo")]


def a_destinatario(cliente):
    """Mapea un cliente de Eryops al destinatario de la agenda.

    Eryops no guarda departamento (sí `ciudad`), así que ese campo queda vacío;
    es opcional en el envío.
    """
    dir_ = (cliente.get("direccion") or "").strip()
    return {
        "nombre": (cliente.get("nombre") or "").strip(),
        "cedula": (cliente.get("documento") or "").strip(),
        "celular": (cliente.get("telefono") or "").strip(),
        "email": (cliente.get("email") or "").strip(),
        "localidad": (cliente.get("ciudad") or "").strip(),
        "entrega_tipo": "direccion" if dir_ else None,
        "entrega_detalle": dir_ or None,
    }
