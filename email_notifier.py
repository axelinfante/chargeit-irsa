"""
Notificaciones por correo (SMTP) para alertas de stock en espirales.
Variables en .env: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, NOTIFICATION_EMAILS.
NOTIFICATION_EMAILS: correos separados por comas donde se envían las alertas.
"""
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger(__name__)

# Variables SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "").strip()
NOTIFICATION_EMAILS_RAW = os.getenv("NOTIFICATION_EMAILS", "").strip()


def get_notification_emails():
    """Lista de correos destino para alertas (separados por comas en .env)."""
    if not NOTIFICATION_EMAILS_RAW:
        return []
    return [e.strip() for e in NOTIFICATION_EMAILS_RAW.split(",") if e.strip()]


def _smtp_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and get_notification_emails())


def _send_email(subject: str, html_body: str) -> bool:
    """Envía un correo HTML a todos los destinatarios de NOTIFICATION_EMAILS."""
    if not _smtp_configured():
        log.warning("SMTP no configurado o sin destinatarios; no se envía correo.")
        return False
    to_list = get_notification_emails()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM or SMTP_USER
        msg["To"] = ", ".join(to_list)
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM or SMTP_USER, to_list, msg.as_string())
        log.info("Correo enviado: %s", subject)
        return True
    except Exception as e:
        log.exception("Error enviando correo: %s", e)
        return False


# Estilo base para los templates (simple y legible)
_STYLE = """
    body { font-family: 'Segoe UI', system-ui, sans-serif; background: #f5f5f5; margin: 0; padding: 24px; }
    .card { max-width: 520px; margin: 0 auto; background: #fff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; }
    .header { background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: #fff; padding: 20px 24px; }
    .header h1 { margin: 0; font-size: 20px; font-weight: 600; }
    .header .sub { margin-top: 4px; font-size: 13px; opacity: 0.9; }
    .body { padding: 24px; color: #333; line-height: 1.5; }
    .body p { margin: 0 0 12px; }
    .highlight { background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; border-radius: 0 8px 8px 0; margin: 16px 0; font-weight: 500; }
    .danger { background: #f8d7da; border-left: 4px solid #dc3545; padding: 12px 16px; border-radius: 0 8px 8px 0; margin: 16px 0; font-weight: 500; }
    .footer { padding: 16px 24px; background: #f8f9fa; font-size: 12px; color: #6c757d; }
"""


def _template_base(title: str, subtitle: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{_STYLE}</style></head>
<body>
  <div class="card">
    <div class="header">
      <h1>{title}</h1>
      <div class="sub">{subtitle}</div>
    </div>
    <div class="body">{body_html}</div>
    <div class="footer">Máquina vending · Notificación automática</div>
  </div>
</body>
</html>"""


def build_template_espiral_cero_stock(espiral_id: str, vending_code: str) -> str:
    """Template: un espiral quedó o está en 0 stock (al probar espirales)."""
    title = "Espiral sin stock"
    subtitle = f"Vending: {vending_code}" if vending_code else "Alerta de stock"
    body = f"""
      <p>Se detectó que el <strong>{espiral_id}</strong> está con <strong>0 stock</strong>.</p>
      <div class="highlight">Conviene reponer producto en este espiral para que la máquina pueda dispensar correctamente.</div>
    """
    return _template_base(title, subtitle, body)


def build_template_espirales_sin_stock(espiral_ids: list, vending_code: str) -> str:
    """Template: varios espirales sin stock (lista)."""
    title = "Espirales sin stock"
    subtitle = f"Vending: {vending_code}" if vending_code else "Alerta de stock"
    lista = ", ".join(espiral_ids) if isinstance(espiral_ids, (list, tuple)) else str(espiral_ids)
    body = f"""
      <p>Los siguientes espirales no tienen stock:</p>
      <div class="highlight">{lista}</div>
      <p>Se recomienda reponer producto en estos espirales.</p>
    """
    return _template_base(title, subtitle, body)


def build_template_vending_sin_stock(vending_code: str) -> str:
    """Template: ningún espiral tiene stock (vending vacío)."""
    title = "Vending sin stock"
    subtitle = f"Vending: {vending_code}" if vending_code else "Alerta crítica"
    body = """
      <p><strong>La máquina expendedora se encuentra sin ningún stock en sus espirales.</strong></p>
      <div class="danger">Ninguno de los espirales tiene producto disponible. Es necesario reponer todos.</div>
      <p>Los intentos de dispensado fallarán hasta que se reponga al menos un espiral.</p>
    """
    return _template_base(title, subtitle, body)


def build_template_stock_threshold(total: int, threshold: int, vending_code: str) -> str:
    """Template: el stock total llegó al umbral definido (ej. quedan solo 15 unidades)."""
    title = "Stock total bajo"
    subtitle = f"Vending: {vending_code}" if vending_code else "Alerta de stock"
    body = f"""
      <p>El stock total de la máquina llegó al umbral configurado.</p>
      <div class="highlight">
        Stock total actual: <strong>{total}</strong><br/>
        Umbral configurado: <strong>{threshold}</strong>
      </div>
      <p>Se recomienda <strong>reponer producto</strong> en los espirales para evitar quedarte sin stock.</p>
    """
    return _template_base(title, subtitle, body)


def notify_espiral_cero_stock(espiral_id: str, vending_code: str = None) -> bool:
    """Notifica que un espiral está en 0 stock (ej. al probar espirales)."""
    vending = vending_code or os.getenv("vendingCode", "")
    html = build_template_espiral_cero_stock(espiral_id, vending)
    return _send_email(f"[Vending] {espiral_id} sin stock", html)


def notify_espirales_sin_stock(espiral_ids: list, vending_code: str = None) -> bool:
    """Notifica qué espirales no tienen stock (lista)."""
    if not espiral_ids:
        return False
    vending = vending_code or os.getenv("vendingCode", "")
    html = build_template_espirales_sin_stock(espiral_ids, vending)
    lista = ", ".join(espiral_ids)
    return _send_email(f"[Vending] Espirales sin stock: {lista}", html)


def notify_vending_sin_stock(vending_code: str = None) -> bool:
    """Notifica que el vending no tiene stock en ninguno de los espirales."""
    vending = vending_code or os.getenv("vendingCode", "")
    html = build_template_vending_sin_stock(vending)
    return _send_email("[Vending] Sin stock en ningún espiral", html)


def notify_smtp_test(vending_code: str = None) -> bool:
    """Correo de prueba para verificar SMTP; no indica alerta de stock."""
    vending = vending_code or os.getenv("vendingCode", "")
    subtitle = f"Vending: {vending}" if vending else "SMTP"
    body = """
      <p>Este es un <strong>correo de prueba</strong> para comprobar que SMTP y destinatarios están bien configurados.</p>
      <p>No indica un problema de stock en la máquina.</p>
    """
    html = _template_base("Prueba SMTP", subtitle, body)
    return _send_email("[Vending] Prueba SMTP", html)


def notify_stock_threshold(total: int, threshold: int, vending_code: str = None) -> bool:
    """Notifica que el stock total alcanzó el umbral configurado (ej. quedan 15 unidades en total)."""
    vending = vending_code or os.getenv("vendingCode", "")
    html = build_template_stock_threshold(total, threshold, vending)
    return _send_email(f"[Vending] Stock total en umbral ({total}/{threshold})", html)
