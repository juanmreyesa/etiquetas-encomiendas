"""App web de etiquetas para encomiendas — Flask.

Open source y agnóstica: marca, remitentes, regiones, agencias, idioma, impresora
y SMTP se configuran desde la Admin UI (/admin) y persisten en la base. Bilingüe
(es/en) vía i18n.py. Ver README.
"""
import csv
import io
import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlparse

from datetime import timedelta

from flask import (Flask, Response, abort, flash, make_response, redirect,
                   render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from PIL import Image, ImageOps

# Cota contra "decompression bombs": rechaza imágenes con demasiados píxeles
# (MAX_CONTENT_LENGTH acota el archivo, no el tamaño descomprimido).
Image.MAX_IMAGE_PIXELS = 64_000_000  # ~64 MP

import crypto
import eryops
import db
import i18n
import mailer
from db import fmt_dt
from labels import LOGOS_DIR, render_label

app = Flask(__name__)
db.init_db()
# La clave de sesión NO puede ser un default público: con ella cualquiera se firma
# una cookie de "logueado". Si no viene por env se genera una al azar y se guarda
# en la base, así sobrevive al reinicio sin quedar en el repo.
app.secret_key = os.environ.get("SECRET_KEY") or db.get_or_create_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # Lax y no Strict: el QR de la etiqueta abre /envios/<id> desde otra app y
    # tiene que llegar logueado. Lax igual no manda la cookie en un POST de otro
    # origen, que es el CSRF que importa acá.
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
app.jinja_env.globals["fmt_dt"] = fmt_dt
# cache-bust del CSS: mtime del archivo → cambia en cada rebuild, invalida el caché del navegador
try:
    _CSS_V = str(int(os.path.getmtime(os.path.join(app.static_folder, "style.css"))))
except OSError:
    _CSS_V = "0"
app.jinja_env.globals["css_v"] = _CSS_V
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB por foto de ticket

# Fotos de tickets: junto a la base, dentro del volumen ./data
TICKETS_DIR = os.path.join(os.path.dirname(db.DB_PATH) or ".", "tickets")
ALLOWED_IMG_EXT = {"jpg", "jpeg", "png", "webp"}


# ---------------- acceso ----------------

# Endpoints que se sirven sin sesión: la propia pantalla de login y los estáticos.
_LIBRES = {"login", "static"}


def auth_activa():
    return bool(db.get_setting("auth_password_hash"))


@app.before_request
def _exigir_login():
    """Contraseña única, opcional. Sin contraseña configurada la app queda como
    estaba (uso LAN sin auth); en cuanto hay una, todo pide sesión."""
    if request.endpoint in _LIBRES or not auth_activa():
        return None
    if session.get("auth"):
        return None
    if request.path.startswith("/api/"):
        abort(401)
    return redirect(url_for("login", next=request.full_path if request.query_string
                            else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_activa():
        return redirect(url_for("index"))
    if request.method == "POST":
        if check_password_hash(db.get_setting("auth_password_hash"),
                               request.form.get("clave", "")):
            session["auth"] = True
            session.permanent = True
            destino = request.form.get("next") or url_for("index")
            # Sólo rutas internas: un `next` absoluto sería un open redirect.
            if not destino.startswith("/") or destino.startswith("//"):
                destino = url_for("index")
            return redirect(destino)
        flash(tr("flash.bad_password"), "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------- idioma / i18n ----------------

def current_lang():
    lang = request.cookies.get("lang") or db.get_setting("language", "es")
    return i18n.normalize_lang(lang)


def tr(key, **kw):
    """Traduce al idioma del request (para flashes y emails server-side)."""
    return i18n.t(key, current_lang(), **kw)


@app.after_request
def _no_cache_html(resp):
    # el HTML es dinámico (estado despachado/pago); evitá que el navegador o el
    # bfcache muestren una lista vieja tras despachar. Estáticos NO se tocan.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.context_processor
def _inject_globals():
    lang = current_lang()
    return {
        "t": lambda key, **kw: i18n.t(key, lang, **kw),
        "lang": lang,
        "languages": i18n.available_languages(),
        "s": db.all_settings(),
        "regions": db.get_regions(),
        "agencies": db.get_agencies(),
        "auth_activa": auth_activa,
    }


@app.route("/idioma/<lang>")
def set_idioma(lang):
    resp = make_response(redirect(_safe_referrer()))
    resp.set_cookie("lang", i18n.normalize_lang(lang),
                    max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


# ---------------- helpers de imagen ----------------

def _normalizar_imagen(stream, dest_dir, basename, max_px, formato):
    """Normaliza una imagen subida (auto-rota por EXIF, achica) y la guarda.
    Devuelve el nombre de archivo. `formato`: 'JPEG' o 'PNG' (preserva alfa)."""
    img = Image.open(stream)
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_px, max_px))
    os.makedirs(dest_dir, exist_ok=True)
    if formato == "PNG":
        img = img.convert("RGBA")
        fname = f"{basename}.png"
        img.save(os.path.join(dest_dir, fname), "PNG", optimize=True)
    else:
        img = img.convert("RGB")
        fname = f"{basename}.jpg"
        img.save(os.path.join(dest_dir, fname), "JPEG", quality=85, optimize=True)
    return fname


def _ext_ok(archivo):
    if not archivo or not archivo.filename:
        return None  # no se subió nada
    ext = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
    return ext if ext in ALLOWED_IMG_EXT else False


def _guardar_ticket(envio_id, archivo):
    """(ok, valor): valor = nombre de archivo, None si no se subió, o mensaje de error."""
    ext = _ext_ok(archivo)
    if ext is None:
        return True, None
    if ext is False:
        return False, tr("flash.img_format")
    try:
        return True, _normalizar_imagen(archivo.stream, TICKETS_DIR,
                                        f"ticket_{envio_id:05d}", 1600, "JPEG")
    except Exception as exc:
        return False, tr("flash.img_process", exc=exc)


def _guardar_logo(archivo, basename):
    """(ok, valor): valor = nombre de archivo o None (no subido) o mensaje error."""
    ext = _ext_ok(archivo)
    if ext is None:
        return True, None
    if ext is False:
        return False, tr("flash.img_format")
    try:
        return True, _normalizar_imagen(archivo.stream, LOGOS_DIR, basename, 600, "PNG")
    except Exception as exc:
        return False, tr("flash.img_process", exc=exc)


# ---------------- impresión + notificación ----------------

def imprimir_pdf(pdf_bytes, titulo, gris=False):
    """Manda el PDF a CUPS con `lp`. Devuelve (ok, mensaje)."""
    printer = db.get_setting("printer", "Epson_L3210")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    opciones = ["-o", "media=A4", "-o", "fit-to-page"]
    if gris:
        # Muchas impresoras (driver escpr de Epson) IGNORAN print-color-mode; su
        # PPD usa el keyword propio `Ink=MONO`. Mandamos ambos por portabilidad.
        opciones += ["-o", "Ink=MONO", "-o", "print-color-mode=monochrome"]
    try:
        res = subprocess.run(
            ["lp", "-d", printer, "-t", titulo] + opciones + [path],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode == 0:
            return True, res.stdout.strip()
        return False, (res.stderr.strip() or res.stdout.strip()
                       or f"lp salió con código {res.returncode}")
    except FileNotFoundError:
        return False, "No se encontró el comando 'lp' (cups-client)."
    except subprocess.TimeoutExpired:
        return False, "Timeout esperando a la impresora."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _delivery_line(envio, lang):
    detail = (envio.get("entrega_detalle") or "").strip()
    if not detail:
        return ""
    key = "email.delivery_agency" if envio["entrega_tipo"] == "agencia" \
        else "email.delivery_home"
    return i18n.t(key, lang, detail=detail)


def _notificar(envio, evento):
    """Avisa por email al receptor (evento: 'print' | 'dispatch'). Best-effort:
    flashea el resultado. No hace nada si SMTP off, sin email, o toggle apagado."""
    to = (envio.get("dest_email") or "").strip()
    flag = "notify_on_print" if evento == "print" else "notify_on_dispatch"
    if not (mailer.smtp_configurado() and to and db.get_bool(flag)):
        return
    lang = current_lang()
    brand = db.get_setting("brand_name") or "Encomiendas"
    n = f"{envio['id']:05d}"
    name = envio.get("dest_nombre") or ""
    dl = _delivery_line(envio, lang)
    adj = adjn = None
    if evento == "print":
        subj = i18n.t("email.print_subject", lang, brand=brand, n=n)
        body = i18n.t("email.print_body", lang, brand=brand, n=n, name=name,
                      delivery_line=dl)
    else:
        date = fmt_dt(envio.get("despachado_at"))
        subj = i18n.t("email.dispatch_subject", lang, brand=brand, n=n)
        body = i18n.t("email.dispatch_body", lang, brand=brand, n=n, name=name,
                      date=date, delivery_line=dl)
        foto = envio.get("ticket_foto")
        if foto:
            p = os.path.join(TICKETS_DIR, os.path.basename(foto))
            if os.path.exists(p):
                adj, adjn = p, foto
    # Cuenta de envío: el override por remitente es TODO-O-NADA — solo se usa si el
    # remitente tiene su propio usuario SMTP (autenticar como X y mandar From Y
    # distinto haría que el correo caiga en spam/sea rechazado). Si no, SMTP global.
    rem = db.get_remitente(envio["rem_id"]) if envio.get("rem_id") else None
    from_addr = smtp_user = smtp_pw = None
    if rem and (rem.get("smtp_user") or "").strip():
        from_addr = rem.get("email_from")
        smtp_user = rem.get("smtp_user")
        smtp_pw = crypto.dec(rem.get("smtp_password"))
    ok, msg = mailer.enviar(to, subj, body, adjunto_path=adj, adjunto_nombre=adjn,
                            from_addr=from_addr, user=smtp_user, password=smtp_pw)
    flash(tr("flash.email_sent") if ok else tr("flash.email_failed", msg=msg),
          "ok" if ok else "error")


# ---------------- remitente ----------------

def resolver_remitente(form):
    rid = form.get("remitente_id")
    rem = db.get_remitente(int(rid)) if rid and rid.isdigit() else None
    if not rem:
        rem = db.remitente_default()
    if not rem:
        return {"rem_nombre": "", "rem_celular": "", "rem_localidad": "",
                "rem_logo": None, "rem_id": None}
    return {"rem_nombre": rem["nombre"], "rem_celular": rem["celular"],
            "rem_localidad": rem["localidad"], "rem_logo": rem["logo"],
            "rem_id": rem["id"]}


@app.errorhandler(413)
def _foto_muy_grande(_):
    flash(tr("flash.img_too_big"), "error")
    return redirect(_safe_referrer())


def _int_arg(name):
    val = request.args.get(name)
    return int(val) if val and val.isdigit() else None


def _safe_referrer():
    """Devuelve request.referrer sólo si es del mismo origen; si no, el index."""
    ref = request.referrer
    if ref and urlparse(ref).netloc == urlparse(request.host_url).netloc:
        return ref
    return url_for("index")


def _csv_cell(v):
    """Mitiga CSV/formula injection: neutraliza celdas que arranquen con = + - @."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


# ---------------- envíos ----------------

@app.route("/")
def index():
    recientes = db.listar_envios(limit=10)
    pre = None
    dup = _int_arg("duplicar")
    dest = _int_arg("destinatario")
    if dup:
        pre = db.get_envio(dup)
    elif dest:
        d = db.get_destinatario(dest)
        if d:
            pre = {
                "dest_nombre": d["nombre"], "dest_cedula": d["cedula"],
                "dest_celular": d["celular"], "dest_departamento": d["departamento"],
                "dest_localidad": d["localidad"], "dest_email": d.get("email"),
                "entrega_tipo": d["entrega_tipo"] or "agencia",
                "entrega_detalle": d["entrega_detalle"],
                # La agenda no guarda quién paga: vale el default del form.
                "paga_destino": 1,
            }
    return render_template("index.html", recientes=recientes, pre=pre,
                           remitentes=db.listar_remitentes(),
                           agendados=db.listar_destinatarios())


@app.route("/envios", methods=["POST"])
def crear():
    nombre = db.norm_nombre(request.form.get("dest_nombre"))
    entrega_tipo = request.form.get("entrega_tipo") or "agencia"
    departamento = (request.form.get("dest_departamento") or "").strip()
    if not nombre:
        flash(tr("flash.name_required"), "error")
        return redirect(url_for("index"))
    origin = (db.get_setting("origin_region") or "").strip()
    if origin and departamento.casefold() == origin.casefold():
        flash(tr("flash.same_origin", region=origin), "error")
        return redirect(url_for("index"))

    data = dict(resolver_remitente(request.form))
    data.update({
        "dest_nombre": nombre,
        # norm_doc y no solo_digitos: un pasaporte ("A22018728") perdía la letra
        # y la etiqueta salía con un documento que no es el de la persona.
        "dest_cedula": db.norm_doc(request.form.get("dest_cedula")),
        "dest_celular": db.norm_tel(request.form.get("dest_celular")),
        "dest_departamento": departamento,
        "dest_localidad": (request.form.get("dest_localidad") or "").strip(),
        "dest_email": db.norm_email(request.form.get("dest_email")),
        "entrega_tipo": entrega_tipo,
        "entrega_detalle": (request.form.get("entrega_detalle") or "").strip(),
        "paga_destino": 1 if request.form.get("paga_destino") else 0,
        "contenido": (request.form.get("contenido") or "").strip(),
        "notas": (request.form.get("notas") or "").strip(),
        "incluir_qr": 1 if request.form.get("incluir_qr") else 0,
        "incluir_firma": 1 if request.form.get("incluir_firma") else 0,
        "gris": 1 if request.form.get("gris") else 0,
    })
    envio_id = db.crear_envio(data)
    if request.form.get("agendar"):
        db.agendar_desde_envio(data)

    if request.form.get("salida") == "pdf":
        return redirect(url_for("pdf", envio_id=envio_id))

    envio = db.get_envio(envio_id)
    ok, msg = imprimir_pdf(render_label(envio, current_lang()),
                           f"#{envio_id}", gris=bool(envio.get("gris")))
    if ok:
        db.marcar_impreso(envio_id, primera=True)
        flash(tr("flash.created_printed", n=f"{envio_id:05d}"), "ok")
        _notificar(db.get_envio(envio_id), "print")
    else:
        flash(tr("flash.created_print_failed", n=f"{envio_id:05d}", msg=msg), "error")
    return redirect(url_for("detalle", envio_id=envio_id))


@app.route("/envios", methods=["GET"])
def historico():
    q = (request.args.get("q") or "").strip()
    envios = db.listar_envios(q=q or None)
    return render_template("historico.html", envios=envios, q=q)


@app.route("/envios.csv")
def historico_csv():
    q = (request.args.get("q") or "").strip()
    envios = db.listar_envios(q=q or None, limit=100000)
    buf = io.StringIO()
    buf.write("﻿")  # BOM para que Excel respete los acentos
    w = csv.writer(buf, delimiter=";")
    cols = ["num", "date", "sender", "sender_phone", "recipient", "id", "phone",
            "email", "region", "locality", "delivery", "detail", "pays_dest",
            "contents", "reprints", "last_printed", "dispatched", "dispatch_date"]
    w.writerow([tr(f"csv.{c}") for c in cols])
    yes, no = tr("csv.yes"), tr("csv.no")
    ag, home = tr("delivery.agency_short"), tr("delivery.home_short")
    for e in envios:
        w.writerow([_csv_cell(c) for c in (
            f"{e['id']:05d}", fmt_dt(e["created_at"]),
            e["rem_nombre"] or "", e["rem_celular"] or "", e["dest_nombre"],
            e["dest_cedula"] or "", e["dest_celular"] or "", e.get("dest_email") or "",
            e["dest_departamento"] or "", e["dest_localidad"] or "",
            ag if e["entrega_tipo"] == "agencia" else home,
            e["entrega_detalle"] or "", yes if e["paga_destino"] else no,
            e["contenido"] or "", e["reimpresiones"],
            fmt_dt(e["last_printed_at"]),
            yes if e["despachado_at"] else no, fmt_dt(e["despachado_at"]),
        )])
    return Response(
        buf.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=encomiendas.csv"})


@app.route("/envios/<int:envio_id>")
def detalle(envio_id):
    envio = db.get_envio(envio_id)
    if not envio:
        abort(404)
    return render_template("detalle.html", e=envio)


@app.route("/envios/<int:envio_id>/reimprimir", methods=["POST"])
def reimprimir(envio_id):
    envio = db.get_envio(envio_id)
    if not envio:
        abort(404)
    ok, msg = imprimir_pdf(render_label(envio, current_lang()),
                           f"#{envio_id}", gris=bool(envio.get("gris")))
    if ok:
        db.marcar_impreso(envio_id, primera=False)
        flash(tr("flash.reprinted"), "ok")
        # No se notifica al receptor en reimpresiones (sólo en la 1ª impresión).
    else:
        flash(tr("flash.reprint_failed", msg=msg), "error")
    return redirect(url_for("detalle", envio_id=envio_id))


@app.route("/envios/<int:envio_id>/despachar", methods=["POST"])
def despachar(envio_id):
    envio = db.get_envio(envio_id)
    if not envio:
        abort(404)
    ok, res = _guardar_ticket(envio_id, request.files.get("ticket"))
    if not ok:
        flash(res, "error")
        return redirect(url_for("detalle", envio_id=envio_id))
    ya_estaba = bool(envio.get("despachado_at"))
    db.marcar_despachado(envio_id, foto=res)
    base = tr("flash.dispatch_updated") if ya_estaba else tr("flash.dispatched")
    flash(base + (tr("flash.photo_saved") if res else ""), "ok")
    if not request.form.get("sin_aviso"):
        _notificar(db.get_envio(envio_id), "dispatch")
    return redirect(url_for("detalle", envio_id=envio_id))


@app.route("/envios/<int:envio_id>/revertir-despacho", methods=["POST"])
def revertir_despacho(envio_id):
    if not db.get_envio(envio_id):
        abort(404)
    db.revertir_despacho(envio_id)
    flash(tr("flash.dispatch_reverted"), "ok")
    return redirect(url_for("detalle", envio_id=envio_id))


@app.route("/envios/<int:envio_id>/ticket")
def ticket_foto(envio_id):
    envio = db.get_envio(envio_id)
    if not envio or not envio.get("ticket_foto"):
        abort(404)
    path = os.path.join(TICKETS_DIR, os.path.basename(envio["ticket_foto"]))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/envios/<int:envio_id>/eliminar", methods=["POST"])
def eliminar(envio_id):
    envio = db.get_envio(envio_id)
    if not envio:
        abort(404)
    foto = envio.get("ticket_foto")
    if foto:
        try:
            os.remove(os.path.join(TICKETS_DIR, os.path.basename(foto)))
        except OSError:
            pass
    db.eliminar_envio(envio_id)
    flash(tr("flash.shipment_deleted", n=f"{envio_id:05d}"), "ok")
    return redirect(url_for("historico"))


@app.route("/envios/<int:envio_id>/pdf")
def pdf(envio_id):
    envio = db.get_envio(envio_id)
    if not envio:
        abort(404)
    pdf_bytes = render_label(envio, current_lang())
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=False, download_name=f"encomienda-{envio_id:05d}.pdf")


@app.route("/api/destinatario")
def api_destinatario():
    r = db.autocompletar_destinatario(
        request.args.get("cedula", ""), request.args.get("celular", ""))
    if not r:
        return {"found": False}
    return {
        "found": True, "dest_nombre": r["nombre"], "dest_cedula": r["cedula"],
        "dest_celular": r["celular"], "dest_departamento": r["departamento"],
        "dest_localidad": r["localidad"], "dest_email": r.get("email"),
        "entrega_tipo": r["entrega_tipo"], "entrega_detalle": r["entrega_detalle"],
    }


# ---------------- agenda de destinatarios ----------------

def _dest_form():
    return {
        "nombre": db.norm_nombre(request.form.get("nombre")),
        "cedula": db.solo_digitos(request.form.get("cedula")),
        "celular": db.norm_tel(request.form.get("celular")),
        "departamento": (request.form.get("departamento") or "").strip(),
        "localidad": (request.form.get("localidad") or "").strip(),
        "email": db.norm_email(request.form.get("email")),
        "entrega_tipo": request.form.get("entrega_tipo") or "agencia",
        "entrega_detalle": (request.form.get("entrega_detalle") or "").strip(),
    }


@app.route("/destinatarios")
def destinatarios():
    q = (request.args.get("q") or "").strip()
    return render_template("destinatarios.html",
                           destinatarios=db.listar_destinatarios(q or None), q=q)


@app.route("/destinatarios/nuevo")
def destinatario_nuevo():
    return render_template("destinatario_form.html", d=None,
                           action=url_for("destinatario_crear"))


@app.route("/destinatarios", methods=["POST"])
def destinatario_crear():
    if not db.norm_nombre(request.form.get("nombre")):
        flash(tr("flash.name_required_generic"), "error")
        return redirect(url_for("destinatario_nuevo"))
    db.crear_destinatario(_dest_form())
    flash(tr("flash.recipient_saved"), "ok")
    return redirect(url_for("destinatarios"))


@app.route("/destinatarios/<int:dest_id>/editar")
def destinatario_editar(dest_id):
    d = db.get_destinatario(dest_id)
    if not d:
        abort(404)
    return render_template("destinatario_form.html", d=d,
                           action=url_for("destinatario_actualizar", dest_id=dest_id))


@app.route("/destinatarios/<int:dest_id>", methods=["POST"])
def destinatario_actualizar(dest_id):
    if not db.get_destinatario(dest_id):
        abort(404)
    if not db.norm_nombre(request.form.get("nombre")):
        flash(tr("flash.name_required_generic"), "error")
        return redirect(url_for("destinatario_editar", dest_id=dest_id))
    db.actualizar_destinatario(dest_id, _dest_form())
    flash(tr("flash.recipient_updated"), "ok")
    return redirect(url_for("destinatarios"))


@app.route("/destinatarios/importar", methods=["POST"])
def importar_clientes():
    """Trae los clientes de Eryops y los mezcla en la agenda (upsert por cédula)."""
    url = db.get_setting("eryops_url")
    usuario = db.get_setting("eryops_user")
    clave = crypto.dec(db.get_setting("eryops_password"))
    if not (url and usuario and clave):
        flash(tr("flash.import_not_configured"), "error")
        return redirect(url_for("destinatarios"))
    try:
        clientes = eryops.traer_clientes(url, usuario, clave)
    except eryops.ErrorEryops as e:
        flash(tr("flash.import_failed", msg=str(e)), "error")
        return redirect(url_for("destinatarios"))
    nuevos = actualizados = omitidos = 0
    for c in clientes:
        d = eryops.a_destinatario(c)
        if not (d["nombre"] and d["cedula"]):
            omitidos += 1          # sin nombre o sin documento no hay con qué agendarlo
            continue
        existente = db.destinatario_por_cedula(d["cedula"])
        if existente:
            # Merge: Eryops no sabe de departamento ni de por dónde se entrega
            # (agencia vs dirección), así que en una ficha que ya existe esos
            # campos son los que mandan y no se pisan.
            fila = dict(existente)
            fila.update({k: v for k, v in d.items()
                         if v and k not in ("entrega_tipo", "entrega_detalle")})
            db.actualizar_destinatario(existente["id"], fila)
            actualizados += 1
        else:
            db.crear_destinatario(d)
            nuevos += 1
    flash(tr("flash.imported", nuevos=nuevos, actualizados=actualizados,
             omitidos=omitidos), "ok")
    if len(clientes) >= eryops.LIMITE:
        flash(tr("flash.import_truncated", n=eryops.LIMITE), "error")
    return redirect(url_for("destinatarios"))


@app.route("/destinatarios/<int:dest_id>/eliminar", methods=["POST"])
def destinatario_eliminar(dest_id):
    db.eliminar_destinatario(dest_id)
    flash(tr("flash.recipient_deleted"), "ok")
    return redirect(url_for("destinatarios"))


# ---------------- remitentes (CRUD admin) ----------------

@app.route("/admin/remitentes")
def remitentes():
    return render_template("remitentes.html", remitentes=db.listar_remitentes())


@app.route("/admin/remitentes/nuevo")
def remitente_nuevo():
    return render_template("remitente_form.html", r=None,
                           action=url_for("remitente_crear"))


def _rem_form_base():
    return {
        "nombre": db.norm_nombre(request.form.get("nombre")),
        "celular": db.norm_tel(request.form.get("celular")),
        "localidad": (request.form.get("localidad") or "").strip(),
        "es_default": 1 if request.form.get("es_default") else 0,
        "email_from": (request.form.get("email_from") or "").strip(),
        "smtp_user": (request.form.get("smtp_user") or "").strip(),
    }


@app.route("/admin/remitentes", methods=["POST"])
def remitente_crear():
    base = _rem_form_base()
    if not base["nombre"]:
        flash(tr("flash.name_required_generic"), "error")
        return redirect(url_for("remitente_nuevo"))
    base["logo"] = None
    base["smtp_password"] = request.form.get("smtp_password") or None
    rem_id = db.crear_remitente(base)
    ok, val = _guardar_logo(request.files.get("logo"), f"sender_{rem_id}")
    if ok and val:
        db.actualizar_remitente(rem_id, {**base, "logo": val})
    flash(tr("flash.sender_saved"), "ok")
    return redirect(url_for("remitentes"))


@app.route("/admin/remitentes/<int:rem_id>/editar")
def remitente_editar(rem_id):
    r = db.get_remitente(rem_id)
    if not r:
        abort(404)
    return render_template("remitente_form.html", r=r,
                           action=url_for("remitente_actualizar", rem_id=rem_id))


@app.route("/admin/remitentes/<int:rem_id>", methods=["POST"])
def remitente_actualizar(rem_id):
    if not db.get_remitente(rem_id):
        abort(404)
    base = _rem_form_base()
    if not base["nombre"]:
        flash(tr("flash.name_required_generic"), "error")
        return redirect(url_for("remitente_editar", rem_id=rem_id))
    ok, val = _guardar_logo(request.files.get("logo"), f"sender_{rem_id}")
    if not ok:
        flash(val, "error")
        return redirect(url_for("remitente_editar", rem_id=rem_id))
    if request.form.get("quitar_logo"):
        base["logo"] = ""          # borra el logo
    elif val:
        base["logo"] = val         # nuevo logo
    else:
        base["logo"] = None        # sin cambios
    # contraseña SMTP del remitente: vacío = no tocar; texto = setear.
    # Si se borró el usuario propio, se limpia la contraseña (no dejar huérfana).
    if not base["smtp_user"]:
        base["smtp_password"] = ""
    else:
        base["smtp_password"] = request.form.get("smtp_password") or None
    db.actualizar_remitente(rem_id, base)
    flash(tr("flash.sender_updated"), "ok")
    return redirect(url_for("remitentes"))


@app.route("/admin/remitentes/<int:rem_id>/eliminar", methods=["POST"])
def remitente_eliminar(rem_id):
    r = db.get_remitente(rem_id)
    if r and r.get("logo"):
        try:
            os.remove(os.path.join(LOGOS_DIR, os.path.basename(r["logo"])))
        except OSError:
            pass
    db.eliminar_remitente(rem_id)
    flash(tr("flash.sender_deleted"), "ok")
    return redirect(url_for("remitentes"))


# ---------------- configuración (Admin UI) ----------------

_SETTING_KEYS = [
    "brand_name", "language", "timezone_offset", "origin_region", "id_validation",
    "printer", "base_url", "color_primary", "color_primary_deep", "color_accent",
    "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from",
    "eryops_url", "eryops_user",
]
_SETTING_BOOLS = ["smtp_enabled", "notify_on_print", "notify_on_dispatch"]


@app.route("/admin")
def admin():
    return render_template("settings_form.html")


@app.route("/admin", methods=["POST"])
def admin_guardar():
    cambios = {}
    for k in _SETTING_KEYS:
        if k in request.form:
            cambios[k] = request.form.get(k, "").strip()
    # colores: se inyectan crudos en un <style>; sólo aceptar #rrggbb válido
    for k in ("color_primary", "color_primary_deep", "color_accent"):
        if k in cambios and not re.fullmatch(r"#[0-9A-Fa-f]{6}", cambios[k]):
            del cambios[k]  # inválido -> conserva el valor actual
    for k in _SETTING_BOOLS:
        cambios[k] = "1" if request.form.get(k) else "0"
    # listas (textarea, una por línea) -> JSON
    for k in ("regions", "agencies"):
        if k in request.form:
            items = [ln.strip() for ln in request.form.get(k, "").splitlines() if ln.strip()]
            cambios[k] = json.dumps(items, ensure_ascii=False)
    # contraseña SMTP: sólo se actualiza si se escribió algo (no se pisa con vacío);
    # se guarda cifrada at-rest.
    pw = request.form.get("smtp_password", "")
    if pw:
        cambios["smtp_password"] = crypto.enc(pw)
    pw = request.form.get("eryops_password", "")
    if pw:
        cambios["eryops_password"] = crypto.enc(pw)
    # Contraseña de acceso: se guarda HASHEADA (nunca en claro, ni cifrada:
    # no hace falta poder leerla de vuelta). Vacío = no tocar.
    pw = request.form.get("auth_password", "")
    if request.form.get("quitar_auth"):
        cambios["auth_password_hash"] = ""
    elif pw:
        cambios["auth_password_hash"] = generate_password_hash(pw)
    # logo de marca
    ok, val = _guardar_logo(request.files.get("logo"), "brand")
    if ok and val:
        cambios["logo"] = val
    elif request.form.get("quitar_logo"):
        cambios["logo"] = ""
    db.set_settings(cambios)
    flash(tr("flash.settings_saved"), "ok")
    return redirect(url_for("admin"))


@app.route("/admin/test-email", methods=["POST"])
def admin_test_email():
    to = (db.get_setting("smtp_user") or db.get_setting("smtp_from") or "").strip()
    if not to:
        flash(tr("flash.test_email_no_addr"), "error")
        return redirect(url_for("admin"))
    lang = current_lang()
    brand = db.get_setting("brand_name") or "Encomiendas"
    ok, msg = mailer.enviar(to, i18n.t("email.test_subject", lang, brand=brand),
                            i18n.t("email.test_body", lang, brand=brand))
    flash(tr("flash.test_email_ok", to=to) if ok else tr("flash.email_failed", msg=msg),
          "ok" if ok else "error")
    return redirect(url_for("admin"))


@app.route("/admin/logo")
def brand_logo():
    name = (db.get_setting("logo") or "").strip()
    if not name:
        abort(404)
    path = os.path.join(LOGOS_DIR, os.path.basename(name))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/admin/remitentes/<int:rem_id>/logo")
def remitente_logo(rem_id):
    r = db.get_remitente(rem_id)
    if not r or not r.get("logo"):
        abort(404)
    path = os.path.join(LOGOS_DIR, os.path.basename(r["logo"]))
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/health")
def health():
    return {"status": "ok", "printer": db.get_setting("printer")}


if __name__ == "__main__":
    # debug solo si se pide explícitamente (el debugger de Werkzeug permite RCE)
    app.run(host="0.0.0.0", port=8000, debug=os.environ.get("FLASK_DEBUG") == "1")
