import db


def test_seed_idempotente():
    db.init_db()
    db.init_db()  # dos veces no debe duplicar
    with db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"]
        dups = conn.execute(
            "SELECT key, COUNT(*) c FROM settings GROUP BY key HAVING c > 1").fetchall()
    assert n == len(db._DEFAULT_SETTINGS)
    assert dups == []


def test_set_get_setting_invalida_cache():
    db.init_db()
    assert db.get_setting("brand_name")  # del seed
    db.set_setting("brand_name", "Acme")
    assert db.get_setting("brand_name") == "Acme"      # cache invalidado
    assert db.all_settings()["brand_name"] == "Acme"


def test_get_bool():
    db.set_setting("smtp_enabled", "1")
    assert db.get_bool("smtp_enabled") is True
    db.set_setting("smtp_enabled", "0")
    assert db.get_bool("smtp_enabled") is False


def test_regions_agencies_parse():
    db.init_db()
    assert "Montevideo" in db.get_regions()
    assert len(db.get_agencies()) > 0


def test_remitentes_crud():
    rid = db.crear_remitente({"nombre": "Acme", "celular": "099", "localidad": "MVD",
                              "logo": None, "es_default": 1})
    r = db.get_remitente(rid)
    assert r["nombre"] == "Acme" and r["es_default"] == 1
    db.actualizar_remitente(rid, {"nombre": "Acme 2", "celular": "098",
                                  "localidad": "MVD", "logo": None, "es_default": 0})
    assert db.get_remitente(rid)["nombre"] == "Acme 2"
    assert db.remitente_default() is not None
    db.eliminar_remitente(rid)
    assert db.get_remitente(rid) is None


def test_norm_helpers():
    assert db.norm_nombre("juan  PEREZ  gómez") == "Juan Perez Gómez"
    assert db.norm_nombre("") == ""
    assert db.solo_digitos("1.234.567-2") == "12345672"
    assert db.norm_tel("098 111 222") == "098111222"
    assert db.norm_tel("+598 99 123 456") == "+59899123456"
