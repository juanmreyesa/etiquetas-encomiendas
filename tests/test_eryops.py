import pytest

import app as appmod
import crypto
import db
import eryops


def test_mapeo_cliente():
    d = eryops.a_destinatario({
        "nombre": " Adriana Musso ", "documento": "29401825", "telefono": "095201991",
        "email": "a@b.com", "direccion": "Japón 1745", "ciudad": "Montevideo",
    })
    assert d == {"nombre": "Adriana Musso", "cedula": "29401825", "celular": "095201991",
                 "email": "a@b.com", "localidad": "Montevideo",
                 "entrega_tipo": "direccion", "entrega_detalle": "Japón 1745"}


def test_mapeo_sin_direccion_no_inventa_entrega():
    d = eryops.a_destinatario({"nombre": "X", "documento": "1", "direccion": ""})
    assert d["entrega_tipo"] is None and d["entrega_detalle"] is None


def test_url_invalida():
    with pytest.raises(eryops.ErrorEryops):
        eryops.traer_clientes("certificados.ejemplo.uy", "u", "c")


def test_login_con_2fa_falla_claro(monkeypatch):
    monkeypatch.setattr(eryops, "_pedir", lambda *a, **k: {"token_2fa": "x"})
    with pytest.raises(eryops.ErrorEryops, match="2FA"):
        eryops.traer_clientes("https://x.uy", "u", "c")


def test_solo_clientes_activos(monkeypatch):
    def fake(url, datos=None, token=None):
        if datos:
            return {"access_token": "t"}
        return [{"nombre": "A", "documento": "1", "activo": True},
                {"nombre": "B", "documento": "2", "activo": False}]
    monkeypatch.setattr(eryops, "_pedir", fake)
    assert [c["nombre"] for c in eryops.traer_clientes("https://x.uy", "u", "c")] == ["A"]


def test_importar_upsert_no_pisa_la_agencia(monkeypatch):
    """Reimportar actualiza el teléfono pero conserva la agencia que ya estaba."""
    db.set_settings({"eryops_url": "https://x.uy", "eryops_user": "svc",
                     "eryops_password": crypto.enc("clave")})
    dest_id = db.crear_destinatario({"nombre": "Viejo", "cedula": "29401825",
                                     "celular": "099", "departamento": "Salto",
                                     "entrega_tipo": "agencia", "entrega_detalle": "DAC Salto"})
    monkeypatch.setattr(eryops, "traer_clientes", lambda *a, **k: [
        {"nombre": "Adriana Musso", "documento": "2.940.182-5", "telefono": "095201991",
         "email": "a@b.com", "ciudad": "Montevideo", "direccion": "Japón 1745", "activo": True},
    ])
    appmod.app.config.update(TESTING=True)
    r = appmod.app.test_client().post("/destinatarios/importar", follow_redirects=True)
    assert r.status_code == 200

    fila = db.get_destinatario(dest_id)
    assert fila["nombre"] == "Adriana Musso"      # se actualiza
    assert fila["celular"] == "095201991"
    assert fila["departamento"] == "Salto"        # Eryops no lo sabe: se conserva
    assert fila["entrega_detalle"] == "DAC Salto" # tampoco se pisa la agencia
    mismos = [d for d in db.listar_destinatarios()
              if db.norm_doc(d["cedula"]) == "29401825"]
    assert len(mismos) == 1                       # upsert por cédula, no duplica


def test_documento_con_letra_no_duplica():
    """Pasaporte 'A22018728': el SQL conserva la letra y solo_digitos la sacaba,
    así que cada importación creaba otra ficha de la misma persona."""
    db.crear_destinatario({"nombre": "Jason Allen", "cedula": "A22018728"})
    assert db.destinatario_por_cedula("A22018728")["nombre"] == "Jason Allen"
    assert db.destinatario_por_cedula("a-22.018.728") is not None
    assert db.destinatario_por_cedula("B22018728") is None


# --- regresiones encontradas auditando el import (2026-09-22) ---

def test_admin_guarda_la_config_de_eryops():
    """El form de /admin renderizaba los campos pero `_SETTING_KEYS` no los tenía:
    se guardaban en la nada y el import decía 'falta configurar' para siempre."""
    appmod.app.config.update(TESTING=True)
    appmod.app.test_client().post("/admin", data={
        "eryops_url": "https://certificados.ejemplo.uy", "eryops_user": "svc",
        "eryops_password": "secreta",
    })
    assert db.get_setting("eryops_url") == "https://certificados.ejemplo.uy"
    assert db.get_setting("eryops_user") == "svc"
    assert crypto.dec(db.get_setting("eryops_password")) == "secreta"


def test_autocomplete_encuentra_pasaporte_y_telefono_con_mas():
    db.crear_destinatario({"nombre": "Jason", "cedula": "A22018728",
                           "celular": "+59899123456"})
    assert db.autocompletar_destinatario(cedula="A22018728")["nombre"] == "Jason"
    assert db.autocompletar_destinatario(cedula="a-22.018.728")["nombre"] == "Jason"
    assert db.autocompletar_destinatario(celular="+598 99 123 456")["nombre"] == "Jason"


def test_precargar_desde_agenda_respeta_paga_destino():
    """`pre` de la agenda no traía la clave y el default se perdía justo en el
    camino más usado (elegir un contacto importado)."""
    dest_id = db.crear_destinatario({"nombre": "X", "cedula": "12345672"})
    appmod.app.config.update(TESTING=True)
    html = appmod.app.test_client().get(f"/?destinatario={dest_id}").get_data(as_text=True)
    marca = html[html.index('name="paga_destino"'):][:60]
    assert "checked" in marca


def test_el_envio_no_le_come_la_letra_al_documento(monkeypatch):
    rem = db.crear_remitente({"nombre": "A", "celular": "1", "localidad": "MVD",
                              "logo": None, "es_default": 1})
    appmod.app.config.update(TESTING=True)
    appmod.app.test_client().post("/envios", data={
        "dest_nombre": "Jason Allen", "dest_cedula": "a22018728",
        "dest_departamento": "Salto", "entrega_tipo": "agencia",
        "entrega_detalle": "DAC", "remitente_id": str(rem), "salida": "pdf",
    })
    assert db.listar_envios(limit=1)[0]["dest_cedula"] == "A22018728"


def test_no_sigue_redirecciones(monkeypatch):
    """El Authorization se re-manda al host nuevo: un 302 regalaría el token."""
    import urllib.error

    def fake_open(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 302, "Found",
                                     {"Location": "https://evil.example/"}, None)
    monkeypatch.setattr(eryops._abridor, "open", fake_open)
    with pytest.raises(eryops.ErrorEryops):
        eryops.traer_clientes("https://x.uy", "u", "c")


def test_el_error_remoto_se_escapa_en_el_html(monkeypatch):
    db.set_settings({"eryops_url": "https://x.uy", "eryops_user": "u",
                     "eryops_password": crypto.enc("c")})
    def boom(*a, **k):
        raise eryops.ErrorEryops("<script>alert(1)</script>")
    monkeypatch.setattr(eryops, "traer_clientes", boom)
    appmod.app.config.update(TESTING=True)
    html = appmod.app.test_client().post("/destinatarios/importar",
                                         follow_redirects=True).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
