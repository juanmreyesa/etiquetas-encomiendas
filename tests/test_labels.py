import labels


def _envio(**extra):
    base = {
        "id": 1, "created_at": "2026-06-15T10:00:00-03:00",
        "rem_nombre": "Acme", "rem_celular": "099", "rem_localidad": "MVD",
        "rem_logo": None, "dest_nombre": "Ana Pérez", "dest_cedula": "12345672",
        "dest_celular": "098111222", "dest_departamento": "Salto",
        "dest_localidad": "Salto", "entrega_tipo": "agencia",
        "entrega_detalle": "DAC", "paga_destino": 0, "contenido": "Una caja",
        "incluir_qr": 1, "incluir_firma": 1,
    }
    base.update(extra)
    return base


def test_pdf_es_en():
    for lang in ("es", "en"):
        out = labels.render_label(_envio(), lang)
        assert out[:4] == b"%PDF" and len(out) > 1000


def test_pdf_paga_destino_sin_qr():
    out = labels.render_label(_envio(paga_destino=1, incluir_qr=0), "es")
    assert out[:4] == b"%PDF"


def test_pdf_logo_inexistente_no_rompe():
    # rem_logo apunta a un archivo que no existe -> se ignora sin excepción
    out = labels.render_label(_envio(rem_logo="no_existe.png"), "en")
    assert out[:4] == b"%PDF"
