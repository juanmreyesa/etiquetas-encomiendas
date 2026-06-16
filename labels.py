"""Generación del PDF de la etiqueta A4 con ReportLab.

Estética minimalista en escala de grises (muchas impresoras ignoran
print-color-mode, así que el formato NO lleva color): tipografía Helvetica con
'kickers' en gris espaciado, una hairline gris y el número en negro. La ÚNICA
excepción es el logo del remitente, que se estampa a color si el remitente tiene
uno cargado. Los textos salen traducidos según el idioma que recibe render_label.
"""
import io
import os

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import db
from db import fmt_dt
from i18n import t

# Los logos subidos viven junto a la base, dentro del volumen ./data.
LOGOS_DIR = os.path.join(os.path.dirname(db.DB_PATH) or ".", "logos")

W, H = A4
MARGIN = 15 * mm


def _qr_image(data):
    qr = qrcode.QRCode(border=1, box_size=10)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _box(c, x, y, w, h):
    c.setStrokeGray(0.18)
    c.setLineWidth(1.0)
    c.rect(x, y, w, h)


def _kicker(c, text, x, y, size=9, gray=0.45):
    """Etiqueta de sección: mayúsculas, gris."""
    c.setFillGray(gray)
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, text.upper())
    c.setFillGray(0)


def _draw_fit(c, text, x, y, font, size, max_w, min_size=10):
    """Dibuja `text` achicando la fuente hasta `min_size` para que entre en
    `max_w`; si aún no entra, recorta con elipsis."""
    text = text or ""
    while size > min_size and c.stringWidth(text, font, size) > max_w:
        size -= 1
    if c.stringWidth(text, font, size) > max_w:
        while text and c.stringWidth(text + "…", font, size) > max_w:
            text = text[:-1]
        text = (text + "…") if text else text
    c.setFont(font, size)
    c.drawString(x, y, text)


def _logo_path(envio):
    name = (envio.get("rem_logo") or "").strip()
    if not name:
        return None
    path = os.path.join(LOGOS_DIR, os.path.basename(name))
    return path if os.path.exists(path) else None


def render_label(envio, lang="es"):
    """Devuelve los bytes del PDF para el dict `envio`, en el idioma `lang`."""
    base_url = (db.get_setting("base_url") or "http://localhost:8088").rstrip("/")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    inner_w = W - 2 * MARGIN

    # ---- Encabezado: título + Nº/fecha + QR ----
    top = H - MARGIN
    c.setFont("Helvetica-Bold", 30)
    c.drawString(MARGIN, top - 22, t("pdf.title", lang))

    if envio.get("incluir_qr", 1):
        qr_size = 28 * mm
        qr = _qr_image(f"{base_url}/envios/{envio['id']}")
        c.drawImage(qr, W - MARGIN - qr_size, top - qr_size,
                    qr_size, qr_size, mask="auto")
        anchor_x = W - MARGIN - qr_size - 6 * mm
    else:
        anchor_x = W - MARGIN

    c.setFillGray(0)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(anchor_x, top - 8, f"{t('pdf.number', lang)} {envio['id']:05d}")
    c.setFillGray(0.45)
    c.setFont("Helvetica", 11)
    c.drawRightString(anchor_x, top - 24, fmt_dt(envio["created_at"]))
    c.setFillGray(0)

    # Hairline gris bajo el encabezado
    c.setStrokeGray(0.18)
    c.setLineWidth(1.4)
    c.line(MARGIN, top - 30 * mm, W - MARGIN, top - 30 * mm)

    y = top - 38 * mm

    # ---- Banner PAGA DESTINO ----
    if envio["paga_destino"]:
        bh = 15 * mm
        c.setFillGray(0)
        c.rect(MARGIN, y - bh, inner_w, bh, fill=1, stroke=0)
        c.setFillGray(1)
        c.setFont("Helvetica-Bold", 25)
        c.drawCentredString(W / 2, y - bh + 4.5 * mm, t("pdf.pays_dest", lang))
        c.setFillGray(0)
        y -= bh + 8 * mm

    # ---- Remitente (con logo si el remitente tiene uno) ----
    rem_nombre = envio.get("rem_nombre") or db.get_setting("brand_name") or ""
    rem_cel = envio.get("rem_celular") or ""
    rem_loc = envio.get("rem_localidad") or ""
    logo = _logo_path(envio)
    rh = 22 * mm
    _box(c, MARGIN, y - rh, inner_w, rh)
    _kicker(c, t("pdf.sender", lang), MARGIN + 4 * mm, y - 7 * mm)

    partes = [p for p in (
        rem_nombre,
        f"{t('pdf.phone_inline', lang)} {rem_cel}" if rem_cel else "",
        rem_loc,
    ) if p]
    linea_rem = "   ·   ".join(partes)
    logo_w = 16 * mm
    rem_max_w = inner_w - 8 * mm - ((logo_w + 5 * mm) if logo else 0)
    _draw_fit(c, linea_rem, MARGIN + 4 * mm, y - 15 * mm, "Helvetica", 14, rem_max_w)
    if logo:
        try:
            c.drawImage(logo, W - MARGIN - 4 * mm - logo_w,
                        y - rh + (rh - logo_w) / 2, logo_w, logo_w,
                        preserveAspectRatio=True, mask=None)
        except Exception:
            pass
    y -= rh + 6 * mm

    # ---- Destinatario (prominente) ----
    dh = 58 * mm
    _box(c, MARGIN, y - dh, inner_w, dh)
    _kicker(c, t("pdf.recipient", lang), MARGIN + 4 * mm, y - 8 * mm)

    _draw_fit(c, envio["dest_nombre"], MARGIN + 4 * mm, y - 20 * mm,
              "Helvetica-Bold", 24, inner_w - 8 * mm, min_size=14)

    c.setFont("Helvetica", 15)
    line_y = y - 32 * mm
    if envio["dest_cedula"]:
        c.drawString(MARGIN + 4 * mm, line_y, f"{t('pdf.id', lang)} {envio['dest_cedula']}")
    if envio["dest_celular"]:
        c.drawString(MARGIN + 95 * mm, line_y, f"{t('pdf.phone', lang)} {envio['dest_celular']}")
    localidad = (envio.get("dest_localidad") or "").strip()
    depto = (envio.get("dest_departamento") or "").strip()
    if localidad and depto:
        destino = f"{t('pdf.destination', lang)} {localidad} ({depto})"
    elif localidad or depto:
        destino = f"{t('pdf.destination', lang)} {localidad or depto}"
    else:
        destino = ""
    if destino:
        _draw_fit(c, destino, MARGIN + 4 * mm, y - 44 * mm, "Helvetica-Bold", 17,
                  inner_w - 8 * mm, min_size=11)
    y -= dh + 6 * mm

    # ---- Entrega ----
    eh = 24 * mm
    _box(c, MARGIN, y - eh, inner_w, eh)
    titulo = t("pdf.pickup", lang) if envio["entrega_tipo"] == "agencia" \
        else t("pdf.home", lang)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(MARGIN + 4 * mm, y - 9 * mm, titulo)
    _draw_fit(c, (envio["entrega_detalle"] or "").strip(),
              MARGIN + 4 * mm, y - 18 * mm, "Helvetica", 14, inner_w - 8 * mm)
    y -= eh + 6 * mm

    # ---- Pie: contenido / firma ----
    if envio.get("contenido"):
        _draw_fit(c, f"{t('pdf.contents', lang)} {envio['contenido']}",
                  MARGIN, y - 6 * mm, "Helvetica", 12, inner_w, min_size=9)
        y -= 10 * mm

    if envio.get("incluir_firma", 1):
        c.setFont("Helvetica", 10)
        c.setFillGray(0.4)
        c.drawString(MARGIN, MARGIN, t("pdf.signature", lang))
        c.setFillGray(0)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()
