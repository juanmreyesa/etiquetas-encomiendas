import i18n


def test_paridad_de_claves():
    es = set(i18n.TRANSLATIONS["es"])
    en = set(i18n.TRANSLATIONS["en"])
    assert es == en, f"faltan: solo_es={es - en} solo_en={en - es}"


def test_interpolacion():
    assert "00007" in i18n.t("flash.shipment_deleted", "es", n="00007")
    assert "00007" in i18n.t("flash.shipment_deleted", "en", n="00007")


def test_fallback_a_espanol_y_a_key():
    # idioma desconocido -> default; key inexistente -> la propia key
    assert i18n.t("nav.new", "xx") == i18n.t("nav.new", "es")
    assert i18n.t("no.existe.key", "en") == "no.existe.key"


def test_kwargs_faltante_no_explota():
    # si falta un kwarg, devuelve el template sin romper
    assert isinstance(i18n.t("flash.same_origin", "es"), str)
