import io

import pytest
from PIL import Image

import app as appmod
import db


@pytest.fixture
def client(monkeypatch):
    # nunca tocar la impresora real
    class _Ok:
        returncode = 0
        stdout = "request id is test-1"
        stderr = ""
    monkeypatch.setattr(appmod.subprocess, "run", lambda *a, **k: _Ok())
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


@pytest.fixture
def remitente():
    return db.crear_remitente({"nombre": "Acme", "celular": "099", "localidad": "MVD",
                               "logo": None, "es_default": 1})


def test_paginas_basicas(client):
    for url in ("/", "/admin", "/destinatarios", "/admin/remitentes", "/envios",
                "/health"):
        assert client.get(url).status_code == 200


def test_crear_envio_normaliza_e_imprime(client, remitente):
    r = client.post("/envios", data={
        "remitente_id": str(remitente), "dest_nombre": "juan  PEREZ",
        "dest_cedula": "1.234.567-2", "dest_celular": "098 111 222",
        "dest_email": "x@example.com", "dest_departamento": "Canelones",
        "dest_localidad": "Pando", "entrega_tipo": "agencia",
        "entrega_detalle": "DAC", "salida": "impresora", "incluir_qr": "on",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    e = db.listar_envios(limit=1)[0]
    assert e["dest_nombre"] == "Juan Perez"
    assert e["dest_cedula"] == "12345672"
    assert e["rem_nombre"] == "Acme"
    # PDF servible
    p = client.get(f"/envios/{e['id']}/pdf")
    assert p.status_code == 200 and p.data[:4] == b"%PDF"


def test_guard_origen_bloquea(client, remitente):
    db.set_setting("origin_region", "Montevideo")
    antes = len(db.listar_envios(limit=100000))
    client.post("/envios", data={
        "remitente_id": str(remitente), "dest_nombre": "X",
        "dest_departamento": "Montevideo", "entrega_tipo": "agencia",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert len(db.listar_envios(limit=100000)) == antes  # no se creó


def test_toggle_idioma(client):
    client.get("/idioma/en")
    assert "New shipment" in client.get("/").get_data(as_text=True)
    client.get("/idioma/es")
    assert "Nuevo envío" in client.get("/").get_data(as_text=True)


def test_csv_tiene_email(client):
    txt = client.get("/envios.csv").get_data(as_text=True)
    assert "Email" in txt.splitlines()[0]


def test_despacho_con_foto_y_email(client, monkeypatch, remitente):
    enviados = []
    monkeypatch.setattr(appmod.mailer, "enviar",
                        lambda *a, **k: (enviados.append(k.get("adjunto_path")), (True, "ok"))[1])
    db.set_settings({"smtp_enabled": "1", "smtp_host": "h", "smtp_from": "a@b.com",
                     "notify_on_dispatch": "1"})
    eid = db.crear_envio({"rem_nombre": "Acme", "dest_nombre": "Ana",
                          "dest_email": "ana@x.com", "dest_cedula": "", "dest_celular": "",
                          "dest_departamento": "Salto", "entrega_tipo": "agencia",
                          "entrega_detalle": "DAC", "paga_destino": 0,
                          "contenido": "", "notas": ""})
    img = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(img, "JPEG")
    img.seek(0)
    r = client.post(f"/envios/{eid}/despachar", data={"ticket": (img, "t.jpg")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    assert db.get_envio(eid)["despachado_at"]
    assert enviados and enviados[-1] is not None  # email con adjunto


def test_color_invalido_no_se_guarda(client):
    db.set_setting("color_primary", "#127C66")
    client.post("/admin", data={"color_primary": "red}body{display:none",
                "language": "es"}, content_type="multipart/form-data",
                follow_redirects=True)
    assert db.get_setting("color_primary") == "#127C66"  # se rechazó el inválido
    client.post("/admin", data={"color_primary": "#abcdef", "language": "es"},
                content_type="multipart/form-data", follow_redirects=True)
    assert db.get_setting("color_primary") == "#abcdef"  # válido sí entra


def test_csv_cell_neutraliza_formulas():
    assert appmod._csv_cell("=HYPERLINK(1)").startswith("'=")
    assert appmod._csv_cell("+1") == "'+1"
    assert appmod._csv_cell("Ana") == "Ana"
    assert appmod._csv_cell(7) == "7"


def test_borrar_remitente_quita_logo(client, tmp_path):
    import io as _io
    from PIL import Image as _Img
    png = _io.BytesIO(); _Img.new("RGBA", (40, 40), (0, 1, 2, 255)).save(png, "PNG"); png.seek(0)
    client.post("/admin/remitentes", data={"nombre": "ConLogo", "logo": (png, "l.png")},
                content_type="multipart/form-data", follow_redirects=True)
    r = [x for x in db.listar_remitentes() if x["nombre"] == "Conlogo"][0]
    import labels, os as _os
    path = _os.path.join(labels.LOGOS_DIR, r["logo"])
    assert _os.path.exists(path)
    client.post(f"/admin/remitentes/{r['id']}/eliminar", follow_redirects=True)
    assert not _os.path.exists(path)


def test_email_usa_cuenta_del_remitente(client, monkeypatch):
    cap = {}
    monkeypatch.setattr(appmod.mailer, "enviar",
                        lambda *a, **k: (cap.update(k), (True, "ok"))[1])
    db.set_settings({"smtp_enabled": "1", "smtp_host": "h", "smtp_user": "global@x.com",
                     "smtp_from": "Global <global@x.com>", "notify_on_print": "1"})
    rid = db.crear_remitente({"nombre": "Eryops", "celular": "099", "localidad": "MVD",
                              "logo": None, "es_default": 0,
                              "email_from": "Enc <enc@eryops.uy>",
                              "smtp_user": "enc@eryops.uy", "smtp_password": "secret"})
    client.post("/envios", data={"remitente_id": str(rid), "dest_nombre": "Ana",
                "dest_email": "ana@x.com", "dest_departamento": "Salto",
                "entrega_tipo": "agencia", "salida": "impresora"},
                content_type="multipart/form-data", follow_redirects=True)
    assert cap.get("from_addr") == "Enc <enc@eryops.uy>"
    assert cap.get("user") == "enc@eryops.uy"
    assert cap.get("password") == "secret"


def test_email_sin_override_usa_global(client, monkeypatch):
    cap = {}
    monkeypatch.setattr(appmod.mailer, "enviar",
                        lambda *a, **k: (cap.update(k), (True, "ok"))[1])
    db.set_settings({"smtp_enabled": "1", "smtp_host": "h", "smtp_user": "global@x.com",
                     "notify_on_print": "1"})
    rid = db.crear_remitente({"nombre": "Simple", "celular": "099", "localidad": "MVD",
                              "logo": None, "es_default": 0})
    client.post("/envios", data={"remitente_id": str(rid), "dest_nombre": "Ana",
                "dest_email": "ana@x.com", "dest_departamento": "Salto",
                "entrega_tipo": "agencia", "salida": "impresora"},
                content_type="multipart/form-data", follow_redirects=True)
    assert not cap.get("from_addr")   # sin override -> mailer cae al global
    assert not cap.get("user")


def test_email_from_sin_user_usa_global(client, monkeypatch):
    # email_from propio pero SIN usuario propio -> override ignorado (usa global)
    cap = {}
    monkeypatch.setattr(appmod.mailer, "enviar",
                        lambda *a, **k: (cap.update(k), (True, "ok"))[1])
    db.set_settings({"smtp_enabled": "1", "smtp_host": "h", "smtp_user": "global@x.com",
                     "notify_on_print": "1"})
    rid = db.crear_remitente({"nombre": "SoloFrom", "celular": "099", "localidad": "MVD",
                              "logo": None, "es_default": 0,
                              "email_from": "X <x@dom>", "smtp_user": "", "smtp_password": ""})
    client.post("/envios", data={"remitente_id": str(rid), "dest_nombre": "Ana",
                "dest_email": "ana@x.com", "dest_departamento": "Salto",
                "entrega_tipo": "agencia", "salida": "impresora"},
                content_type="multipart/form-data", follow_redirects=True)
    assert not cap.get("from_addr") and not cap.get("user")


def test_borrar_user_remitente_limpia_password(client):
    rid = db.crear_remitente({"nombre": "ConCuenta", "celular": "099", "localidad": "MVD",
                              "logo": None, "es_default": 0, "email_from": "X <x@dom>",
                              "smtp_user": "x@dom", "smtp_password": "secret"})
    assert db.get_remitente(rid)["smtp_password"] == "secret"
    client.post(f"/admin/remitentes/{rid}", data={"nombre": "ConCuenta",
                "smtp_user": "", "email_from": "", "smtp_password": ""},
                content_type="multipart/form-data", follow_redirects=True)
    assert db.get_remitente(rid)["smtp_password"] == ""   # password huérfana limpiada


def test_mailer_sanea_crlf_en_cabeceras():
    assert "\n" not in appmod.mailer._hdr("a@x.com\nBcc: y@z.com")
    assert "\r" not in appmod.mailer._hdr("a@x.com\r\nSubject: x")
    assert db.norm_email("  a@x.com\n ") == "a@x.com"


def test_remitente_crud_via_http(client):
    r = client.post("/admin/remitentes", data={"nombre": "transportes vega",
                    "celular": "099", "localidad": "MVD"},
                    content_type="multipart/form-data", follow_redirects=True)
    assert r.status_code == 200
    assert any(x["nombre"] == "Transportes Vega" for x in db.listar_remitentes())
