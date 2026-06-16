"""Acceso a SQLite: histórico de envíos, agenda, remitentes y configuración.

La configuración (marca, idioma, regiones, agencias, impresora, SMTP, etc.) vive
en la tabla `settings` (clave/valor) y se siembra con defaults en el primer
arranque; todo es editable desde la Admin UI sin tocar código ni redeploy.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("ETIQUETAS_DB", "/data/etiquetas.db")

# Defaults sembrados en la primera ejecución (preset Uruguay). Editables luego
# desde /admin; NO son la fuente de verdad en runtime (la tabla settings lo es).
_DEFAULT_REGIONS = [
    "Artigas", "Canelones", "Cerro Largo", "Colonia", "Durazno", "Flores",
    "Florida", "Lavalleja", "Maldonado", "Montevideo", "Paysandú",
    "Río Negro", "Rivera", "Rocha", "Salto", "San José", "Soriano",
    "Tacuarembó", "Treinta y Tres",
]
_DEFAULT_AGENCIES = [
    "DAC", "Nordeste", "Turismar", "Agencia Central", "COT", "Núñez",
    "EGA", "Cynsa", "Copay", "Rutas del Sol", "CUT Corporation",
    "Chadre", "Agencia Minuano", "Sabelín", "Cita",
]

# Valores por defecto de cada setting. El seed los inserta con INSERT OR IGNORE,
# así que agregar una clave nueva acá la siembra sin migración de esquema.
_DEFAULT_SETTINGS = {
    "brand_name": "Encomiendas",
    "language": "es",
    "timezone_offset": "-3",          # offset UTC en horas (acepta decimales)
    "origin_region": "Montevideo",    # vacío = no se bloquea ningún destino
    "id_validation": "uy_ci",         # uy_ci | none
    "printer": os.environ.get("PRINTER", "Epson_L3210"),
    "base_url": os.environ.get("BASE_URL", "http://localhost:8088"),
    "color_primary": "#127C66",
    "color_primary_deep": "#0E6552",
    "color_accent": "#B5872E",
    "logo": "",                       # nombre de archivo en data/logos (marca)
    "regions": json.dumps(_DEFAULT_REGIONS, ensure_ascii=False),
    "agencies": json.dumps(_DEFAULT_AGENCIES, ensure_ascii=False),
    # --- Notificaciones por email al receptor (opt-in, apagado por defecto) ---
    "smtp_enabled": "0",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_security": "starttls",      # starttls | ssl | none
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",                  # ej. "Encomiendas <envios@dominio>"
    "notify_on_print": "1",           # avisar al imprimir la etiqueta
    "notify_on_dispatch": "1",        # avisar al marcar despachado (adjunta ticket)
}

# Cache de settings con TTL corto. Con gunicorn multi-worker cada proceso tiene el
# suyo; el TTL acota la ventana de staleness tras editar en otro worker. SQLite
# local es barato, así que el TTL es solo para no releer en cada acceso del request.
_SETTINGS_TTL = 2.0  # segundos
_settings_cache = {"data": None, "ts": 0.0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS envios (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    rem_nombre        TEXT,
    rem_celular       TEXT,
    dest_nombre       TEXT NOT NULL,
    dest_cedula       TEXT,
    dest_celular      TEXT,
    dest_departamento TEXT,
    entrega_tipo      TEXT NOT NULL,
    entrega_detalle   TEXT,
    paga_destino      INTEGER NOT NULL DEFAULT 0,
    contenido         TEXT,
    notas             TEXT,
    incluir_qr        INTEGER NOT NULL DEFAULT 1,
    incluir_firma     INTEGER NOT NULL DEFAULT 1,
    gris              INTEGER NOT NULL DEFAULT 0,
    reimpresiones     INTEGER NOT NULL DEFAULT 0,
    last_printed_at   TEXT
);

CREATE TABLE IF NOT EXISTS destinatarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    nombre          TEXT NOT NULL,
    cedula          TEXT,
    celular         TEXT,
    departamento    TEXT,
    entrega_tipo    TEXT,
    entrega_detalle TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS remitentes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    nombre     TEXT NOT NULL,
    celular    TEXT,
    localidad  TEXT,            -- localidad de origen (ej. Montevideo)
    logo       TEXT,            -- nombre de archivo en data/logos (o NULL)
    es_default INTEGER NOT NULL DEFAULT 0
);
"""

# columnas agregadas después del esquema inicial -> migración idempotente
_COLUMNAS_EXTRA = [
    ("rem_nombre", "TEXT"),
    ("rem_celular", "TEXT"),
    ("rem_localidad", "TEXT"),   # localidad de origen del remitente
    ("rem_logo", "TEXT"),        # logo del remitente al momento del envío
    ("incluir_qr", "INTEGER NOT NULL DEFAULT 1"),
    ("incluir_firma", "INTEGER NOT NULL DEFAULT 1"),
    ("gris", "INTEGER NOT NULL DEFAULT 0"),
    ("despachado_at", "TEXT"),   # NULL = sin despachar; ISO = fecha del despacho
    ("ticket_foto", "TEXT"),     # nombre del archivo en data/tickets (o NULL)
    ("dest_localidad", "TEXT"),  # ciudad/localidad de destino (ej. Sarandí del Yí)
    ("dest_email", "TEXT"),      # email del receptor (para notificaciones)
]

# Igual, para la agenda de destinatarios (mantener paridad con envios)
_COLUMNAS_EXTRA_DEST = [
    ("localidad", "TEXT"),
    ("email", "TEXT"),
]


def _tz():
    """Zona horaria del setting `timezone_offset` (offset UTC en horas)."""
    try:
        return timezone(timedelta(hours=float(get_setting("timezone_offset", "-3"))))
    except (TypeError, ValueError):
        return timezone(timedelta(hours=-3))


def now_iso():
    return datetime.now(_tz()).isoformat(timespec="seconds")


def norm_nombre(s):
    """Normaliza nombres/apellidos: capitaliza la primera letra de cada palabra
    (unicode-aware), colapsa espacios. 'JUAN  perez' -> 'Juan Perez'."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in (s or "").split())


def norm_email(s):
    return (s or "").strip()


def norm_tel(s):
    """Normaliza teléfonos: conserva un '+' inicial y deja sólo dígitos."""
    s = (s or "").strip()
    digits = solo_digitos(s)
    return ("+" + digits) if s.startswith("+") and digits else digits


def fmt_dt(iso_str):
    """ISO -> 'dd/mm/YYYY HH:MM' para mostrar."""
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso_str


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
        for col, ddl in _COLUMNAS_EXTRA:
            if col not in cols:
                conn.execute(f"ALTER TABLE envios ADD COLUMN {col} {ddl}")
        cols_d = {r["name"] for r in conn.execute("PRAGMA table_info(destinatarios)")}
        for col, ddl in _COLUMNAS_EXTRA_DEST:
            if col not in cols_d:
                conn.execute(f"ALTER TABLE destinatarios ADD COLUMN {col} {ddl}")
        # Seed idempotente de settings: solo inserta claves que falten (no pisa
        # ediciones del usuario ni datos viejos).
        for key, val in _DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                         (key, val))
    _settings_cache["data"] = None  # invalida cache tras posibles cambios


def crear_envio(data):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO envios
               (created_at, rem_nombre, rem_celular, rem_localidad, rem_logo,
                dest_nombre, dest_cedula, dest_celular, dest_departamento,
                dest_localidad, dest_email, entrega_tipo, entrega_detalle,
                paga_destino, contenido, notas, incluir_qr, incluir_firma, gris)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                now_iso(),
                data.get("rem_nombre"), data.get("rem_celular"),
                data.get("rem_localidad"), data.get("rem_logo"),
                data["dest_nombre"], data["dest_cedula"], data["dest_celular"],
                data["dest_departamento"], data.get("dest_localidad"),
                data.get("dest_email"),
                data["entrega_tipo"], data["entrega_detalle"],
                int(data["paga_destino"]),
                data["contenido"], data["notas"],
                int(data.get("incluir_qr", 1)), int(data.get("incluir_firma", 1)),
                int(data.get("gris", 0)),
            ),
        )
        return cur.lastrowid


def get_envio(envio_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM envios WHERE id=?", (envio_id,)).fetchone()
        return dict(row) if row else None


def marcar_impreso(envio_id, primera=False):
    with get_conn() as conn:
        if primera:
            conn.execute(
                "UPDATE envios SET last_printed_at=? WHERE id=?",
                (now_iso(), envio_id),
            )
        else:
            conn.execute(
                "UPDATE envios SET reimpresiones=reimpresiones+1, last_printed_at=? "
                "WHERE id=?",
                (now_iso(), envio_id),
            )


def marcar_despachado(envio_id, foto=None):
    """Marca el envío como despachado. La fecha del PRIMER despacho se conserva
    (COALESCE), así re-enviar el form para adjuntar/cambiar la foto no la pisa.
    Si `foto` no es None, asocia/reemplaza el nombre de archivo del ticket."""
    with get_conn() as conn:
        if foto is not None:
            conn.execute(
                "UPDATE envios SET despachado_at=COALESCE(despachado_at, ?), "
                "ticket_foto=? WHERE id=?",
                (now_iso(), foto, envio_id),
            )
        else:
            conn.execute(
                "UPDATE envios SET despachado_at=COALESCE(despachado_at, ?) WHERE id=?",
                (now_iso(), envio_id),
            )


def revertir_despacho(envio_id):
    """Quita la marca de despachado (conserva la foto del ticket si la hubiera)."""
    with get_conn() as conn:
        conn.execute("UPDATE envios SET despachado_at=NULL WHERE id=?", (envio_id,))


def eliminar_envio(envio_id):
    """Borra el envío. La foto del ticket en disco la borra el caller (app)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM envios WHERE id=?", (envio_id,))


# Reconstruye la fecha como dd/mm/aaaa por slicing del ISO (sin conversión de
# zona horaria) para poder buscar por fecha tal como se muestra.
_FECHA_DDMMAAAA = ("substr(created_at,9,2)||'/'||substr(created_at,6,2)"
                   "||'/'||substr(created_at,1,4)")


def listar_envios(q=None, limit=200):
    sql = "SELECT * FROM envios"
    params = []
    if q:
        sql += (" WHERE dest_nombre LIKE ? OR dest_cedula LIKE ? "
                "OR dest_departamento LIKE ? OR created_at LIKE ? "
                f"OR {_FECHA_DDMMAAAA} LIKE ?")
        like = f"%{q}%"
        params = [like, like, like, like, like]
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def solo_digitos(s):
    return "".join(ch for ch in (s or "") if ch.isdigit())


# Normaliza una columna a sólo-dígitos dentro del SQL (la columna es un literal
# controlado, nunca entrada del usuario -> seguro).
def _norm(col):
    return f"REPLACE(REPLACE(REPLACE({col},'.',''),'-',''),' ','')"


def autocompletar_destinatario(cedula="", celular=""):
    """Trae datos de un destinatario por cédula o celular (sólo-dígitos),
    buscando primero en la AGENDA y luego en envíos previos. Devuelve un dict
    con claves uniformes (nombre, cedula, celular, departamento, entrega_*)."""
    ced = solo_digitos(cedula)
    cel = solo_digitos(celular)
    if not ced and not cel:
        return None
    sel_ag = ("SELECT nombre, cedula, celular, departamento, localidad, email, "
              "entrega_tipo, entrega_detalle FROM destinatarios WHERE {cond}=? "
              "ORDER BY id DESC LIMIT 1")
    sel_en = ("SELECT dest_nombre AS nombre, dest_cedula AS cedula, "
              "dest_celular AS celular, dest_departamento AS departamento, "
              "dest_localidad AS localidad, dest_email AS email, "
              "entrega_tipo, entrega_detalle "
              "FROM envios WHERE {cond}=? ORDER BY id DESC LIMIT 1")
    intentos = []
    if ced:
        intentos.append((sel_ag.format(cond=_norm("cedula")), ced))
    if cel:
        intentos.append((sel_ag.format(cond=_norm("celular")), cel))
    if ced:
        intentos.append((sel_en.format(cond=_norm("dest_cedula")), ced))
    if cel:
        intentos.append((sel_en.format(cond=_norm("dest_celular")), cel))
    with get_conn() as conn:
        for sql, val in intentos:
            row = conn.execute(sql, (val,)).fetchone()
            if row:
                return dict(row)
    return None


# ---------------- Agenda de destinatarios ----------------

def crear_destinatario(d):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO destinatarios "
            "(created_at, nombre, cedula, celular, departamento, localidad, email, entrega_tipo, entrega_detalle) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), d["nombre"], d.get("cedula"), d.get("celular"),
             d.get("departamento"), d.get("localidad"), d.get("email"),
             d.get("entrega_tipo"), d.get("entrega_detalle")),
        )
        return cur.lastrowid


def actualizar_destinatario(dest_id, d):
    with get_conn() as conn:
        conn.execute(
            "UPDATE destinatarios SET nombre=?, cedula=?, celular=?, departamento=?, "
            "localidad=?, email=?, entrega_tipo=?, entrega_detalle=? WHERE id=?",
            (d["nombre"], d.get("cedula"), d.get("celular"), d.get("departamento"),
             d.get("localidad"), d.get("email"), d.get("entrega_tipo"),
             d.get("entrega_detalle"), dest_id),
        )


def get_destinatario(dest_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM destinatarios WHERE id=?", (dest_id,)).fetchone()
        return dict(row) if row else None


def listar_destinatarios(q=None):
    sql = "SELECT * FROM destinatarios"
    params = []
    if q:
        sql += " WHERE nombre LIKE ? OR cedula LIKE ? OR departamento LIKE ?"
        like = f"%{q}%"
        params = [like, like, like]
    sql += " ORDER BY nombre COLLATE NOCASE"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def eliminar_destinatario(dest_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM destinatarios WHERE id=?", (dest_id,))


def _destinatario_por_cedula(cedula):
    digits = solo_digitos(cedula)
    if not digits:
        return None
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT * FROM destinatarios WHERE {_norm('cedula')} = ? "
            "ORDER BY id DESC LIMIT 1",
            (digits,),
        ).fetchone()
        return dict(row) if row else None


def agendar_desde_envio(data):
    """Guarda/actualiza un destinatario a partir de los datos de un envío.
    Si hay cédula y ya existe, lo actualiza; si no, lo crea."""
    d = {
        "nombre": data["dest_nombre"],
        "cedula": data.get("dest_cedula"),
        "celular": data.get("dest_celular"),
        "departamento": data.get("dest_departamento"),
        "localidad": data.get("dest_localidad"),
        "email": data.get("dest_email"),
        "entrega_tipo": data.get("entrega_tipo"),
        "entrega_detalle": data.get("entrega_detalle"),
    }
    existente = _destinatario_por_cedula(d["cedula"]) if d["cedula"] else None
    if existente:
        actualizar_destinatario(existente["id"], d)
        return existente["id"]
    return crear_destinatario(d)


# ---------------- Configuración (settings) ----------------

def all_settings():
    """Devuelve todos los settings como dict {clave: valor}, con cache TTL."""
    now = time.monotonic()
    cached = _settings_cache["data"]
    if cached is not None and (now - _settings_cache["ts"]) < _SETTINGS_TTL:
        return cached
    data = dict(_DEFAULT_SETTINGS)  # fallback si la tabla aún no existe
    try:
        with get_conn() as conn:
            for r in conn.execute("SELECT key, value FROM settings"):
                data[r["key"]] = r["value"]
    except sqlite3.OperationalError:
        pass  # tabla todavía no creada (durante init)
    _settings_cache["data"] = data
    _settings_cache["ts"] = now
    return data


def get_setting(key, default=None):
    val = all_settings().get(key)
    if val is None:
        return _DEFAULT_SETTINGS.get(key, default)
    return val


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )
    _settings_cache["data"] = None  # invalida


def set_settings(mapping):
    for k, v in mapping.items():
        set_setting(k, v)


def get_bool(key):
    return str(get_setting(key, "0")).strip() in ("1", "true", "True", "on", "yes")


def _parse_lines_or_json(raw):
    """Acepta JSON (como se guarda) o texto con una entrada por línea (del form)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except (ValueError, TypeError):
            pass
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def get_regions():
    return _parse_lines_or_json(get_setting("regions"))


def get_agencies():
    return _parse_lines_or_json(get_setting("agencies"))


# ---------------- Remitentes (CRUD, igual patrón que destinatarios) ----------------

def crear_remitente(r):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO remitentes (created_at, nombre, celular, localidad, logo, es_default) "
            "VALUES (?,?,?,?,?,?)",
            (now_iso(), r["nombre"], r.get("celular"), r.get("localidad"),
             r.get("logo"), int(r.get("es_default") or 0)),
        )
        return cur.lastrowid


def actualizar_remitente(rem_id, r):
    with get_conn() as conn:
        # logo=None => no se toca (no se subió uno nuevo); "" => se borra
        if r.get("logo") is None:
            conn.execute(
                "UPDATE remitentes SET nombre=?, celular=?, localidad=?, es_default=? WHERE id=?",
                (r["nombre"], r.get("celular"), r.get("localidad"),
                 int(r.get("es_default") or 0), rem_id),
            )
        else:
            conn.execute(
                "UPDATE remitentes SET nombre=?, celular=?, localidad=?, logo=?, es_default=? WHERE id=?",
                (r["nombre"], r.get("celular"), r.get("localidad"), r["logo"],
                 int(r.get("es_default") or 0), rem_id),
            )


def get_remitente(rem_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM remitentes WHERE id=?", (rem_id,)).fetchone()
        return dict(row) if row else None


def listar_remitentes():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM remitentes ORDER BY es_default DESC, nombre COLLATE NOCASE")]


def remitente_default():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM remitentes ORDER BY es_default DESC, id ASC LIMIT 1").fetchone()
        return dict(row) if row else None


def eliminar_remitente(rem_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM remitentes WHERE id=?", (rem_id,))
