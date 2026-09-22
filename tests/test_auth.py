import pytest

import app as appmod
import db


@pytest.fixture(autouse=True)
def _sin_clave_al_salir():
    """Dejar la contraseña puesta cerraría la app para los tests siguientes."""
    yield
    db.set_settings({"auth_password_hash": ""})


def _client():
    appmod.app.config.update(TESTING=True)
    return appmod.app.test_client()


def _con_clave(clave="secreta"):
    from werkzeug.security import generate_password_hash
    db.set_settings({"auth_password_hash": generate_password_hash(clave)})


def test_sin_contrasena_la_app_queda_abierta():
    db.set_settings({"auth_password_hash": ""})
    assert _client().get("/").status_code == 200
    assert _client().get("/login").status_code == 302     # no hay nada que pedir


def test_con_contrasena_todo_pide_sesion():
    _con_clave()
    c = _client()
    for ruta in ("/", "/destinatarios", "/envios", "/admin", "/envios.csv"):
        r = c.get(ruta)
        assert r.status_code == 302 and "/login" in r.headers["Location"], ruta


def test_post_sin_sesion_no_ejecuta_nada():
    """Lo importante no es el 302: es que la acción NO ocurra."""
    _con_clave()
    antes = len(db.listar_destinatarios())
    r = _client().post("/destinatarios", data={"nombre": "Colado"})
    assert r.status_code == 302
    assert len(db.listar_destinatarios()) == antes


def test_la_api_devuelve_401_y_no_datos():
    _con_clave()
    db.crear_destinatario({"nombre": "Secreto", "cedula": "12345672"})
    r = _client().get("/api/destinatario?cedula=12345672")
    assert r.status_code == 401
    assert b"Secreto" not in r.data


def test_login_ok_y_clave_incorrecta():
    _con_clave("buena")
    c = _client()
    assert c.post("/login", data={"clave": "mala"}).status_code == 200   # vuelve al form
    assert c.get("/").status_code == 302                                  # sigue afuera
    assert c.post("/login", data={"clave": "buena"}).status_code == 302
    assert c.get("/").status_code == 200


def test_next_no_permite_open_redirect():
    _con_clave()
    c = _client()
    r = c.post("/login", data={"clave": "secreta", "next": "https://evil.example/x"})
    assert r.headers["Location"] in ("/", "http://localhost/")
    r = c.post("/login", data={"clave": "secreta", "next": "//evil.example"})
    assert "evil" not in r.headers["Location"]


def test_logout_corta_la_sesion():
    _con_clave()
    c = _client()
    c.post("/login", data={"clave": "secreta"})
    assert c.get("/").status_code == 200
    c.post("/logout")
    assert c.get("/").status_code == 302


def test_la_cookie_es_httponly_y_samesite_lax():
    """SameSite=Lax es lo que bloquea el POST cross-site (el CSRF que importa)."""
    _con_clave()
    c = _client()
    r = c.post("/login", data={"clave": "secreta"})
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_la_clave_no_se_guarda_en_claro():
    _con_clave("secreta")
    guardado = db.get_setting("auth_password_hash")
    assert "secreta" not in guardado and guardado.count("$") >= 2


def test_secreto_de_sesion_estable_y_no_adivinable():
    a = db.get_or_create_secret()
    assert a == db.get_or_create_secret() and len(a) >= 32
