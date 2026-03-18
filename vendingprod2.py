import asyncio
import flet as ft
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, date
import subprocess

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import RPi.GPIO as GPIO
import board
import busio
import adafruit_adxl34x
import pygame

try:
    from firestore_config import (
        get_firestore,
        get_config_stock,
        update_config_stock,
        registrar_evento_history,
        get_history_by_date_range,
    )
    _firestore_import_error = None
except Exception as _firestore_import_error:
    get_firestore = get_config_stock = update_config_stock = registrar_evento_history = get_history_by_date_range = None

try:
    from email_notifier import (
        notify_espiral_cero_stock,
        notify_espirales_sin_stock,
        notify_vending_sin_stock,
        notify_stock_threshold,
    )
    _email_notifier_available = True
    _email_notifier_error = None
except Exception as _email_notifier_err:
    notify_espiral_cero_stock = notify_espirales_sin_stock = notify_vending_sin_stock = notify_stock_threshold = None
    _email_notifier_available = False
    _email_notifier_error = _email_notifier_err

# ==============================
# LOGGING
# ==============================
LOGS_DIR = "logs"
os.makedirs(LOGS_DIR, exist_ok=True)
_log_filename = os.path.join(LOGS_DIR, f"vending_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log")
_log_format = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=_log_format,
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_filename, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ==============================
# SONIDO (bip de confirmación)
# ==============================
pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=512)
pygame.mixer.init()

FRECUENCIA_BIP   = 1200
DURACION_BIP     = 0.18
REPETICIONES     = 2
PAUSA_ENTRE      = 0.07
VOLUMEN_PYGAME   = 1.0

sample_rate = 44100
periodo = int(sample_rate / FRECUENCIA_BIP)
half_period = periodo // 2

samples = bytearray()
for i in range(int(sample_rate * DURACION_BIP)):
    if (i % periodo) < half_period:
        value = 32767
    else:
        value = -32768
    samples.extend([value & 0xFF, (value >> 8) & 0xFF])

bip_sound = pygame.mixer.Sound(buffer=samples)
bip_sound.set_volume(VOLUMEN_PYGAME)

log.info("Prueba de sonido inicial...")
bip_sound.play()
time.sleep(0.8)

# ==============================
# CONFIGURACIÓN GPIO
# ==============================
RELAY_PINS = [4, 6, 22, 26]
TIEMPO_GIRO = 1

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in RELAY_PINS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

# ==============================
# VARIABLES GLOBALES
# ==============================
STOCK = {}
CODIGO_ADMIN = os.getenv("CODIGO_ADMIN", "admin1234")
URL_API = (os.getenv("url_api") or "").rstrip("/")
STORE_ID = os.getenv("storeId", "")
API_KEY = os.getenv("x-api-key", "")
VENDING_CODE = os.getenv("vendingCode", "")
# Umbral de stock total (suma de todos los espirales) para enviar alerta por email
try:
    MAX_STOCK_PER_SPIRAL = int(os.getenv("MAX_STOCK_PER_SPIRAL", "15") or "15")
except ValueError:
    MAX_STOCK_PER_SPIRAL = 15

FONDO_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "fondo.jpg")

page = None
_alert_firestore = None
accel = None

ESPIRAL_ORDER = ["espiral1", "espiral2", "espiral3", "espiral4"]

DEBOUNCE_SEC = 1.2
last_detection_time = 0

# ==============================
# DETECCIÓN DE IMPACTO (ADXL345)
# ==============================
def esperar_deteccion(timeout=7.0):
    global last_detection_time

    if accel is None:
        log.warning("ADXL345 no disponible → se asume dispensado OK")
        time.sleep(TIEMPO_GIRO + 0.5)
        return True

    try:
        _ = accel.events['tap']
    except:
        pass

    inicio = time.time()
    log.info(f"Esperando impacto ADXL345... (máx {timeout:.1f} seg)")

    while time.time() - inicio < timeout:
        try:
            if accel.events['tap']:
                now = time.time()
                if now - last_detection_time > DEBOUNCE_SEC:
                    log.info("¡IMPACTO DETECTADO! (Single Tap)")
                    last_detection_time = now
                    _ = accel.events['tap']
                    for _ in range(REPETICIONES):
                        bip_sound.play()
                        time.sleep(PAUSA_ENTRE)
                    return True
        except Exception as e:
            log.error(f"Error leyendo tap event: {e}")
        time.sleep(0.02)

    log.warning("No se detectó impacto en el tiempo permitido")
    return False

# ==============================
# ACTIVAR RELAY
# ==============================
def activar_relay(idx):
    pin = RELAY_PINS[idx]
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(TIEMPO_GIRO)
    GPIO.output(pin, GPIO.LOW)

# ==============================
# API VALIDACIÓN Y REDENCIÓN
# ==============================
def validar_codigo_api(codigo):
    if not URL_API or not STORE_ID or not API_KEY:
        return False, "API no configurada (revisá .env)"
    url = f"{URL_API}/location/{STORE_ID}/redemption-codes/{codigo.strip()}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("x-api-key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                log.info(f"Código válido: benefitId={data.get('benefitId')}")
                return True, None
            return False, "Respuesta inesperada"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "Código inválido o ya usado"
        return False, f"Error HTTP {e.code}"
    except Exception as e:
        log.warning(f"Error validando código: {e}")
        return False, "Error de conexión"

def redimir_codigo_api(codigo, vending_code):
    if not all([URL_API, STORE_ID, API_KEY, vending_code]):
        return False, "Configuración incompleta"
    url = f"{URL_API}/location/{STORE_ID}/redemption-codes/{codigo.strip()}/redemptions"
    body = json.dumps({"vendingCode": vending_code.strip()}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 201), None
    except Exception as e:
        log.warning(f"Error al redimir: {e}")
        return False, "No se pudo redimir"

# ==============================
# UTILIDADES DE STOCK
# ==============================
def _get_total_stock_actual():
    """Devuelve la suma del stock actual conocido en memoria para los espirales definidos en ESPIRAL_ORDER."""
    total = 0
    for esp in ESPIRAL_ORDER:
        try:
            total += int(STOCK.get(esp, 0) or 0)
        except (ValueError, TypeError):
            pass
    return total


def _check_stock_threshold_and_notify():
    """
    Si el stock total actual coincide con el umbral MAX_STOCK_PER_SPIRAL, envía una notificación por email.
    Se usa como indicador de que quedan pocas unidades en total.
    """
    if not (_email_notifier_available and notify_stock_threshold):
        return
    try:
        total = _get_total_stock_actual()
    except Exception as ex:
        log.warning("No se pudo calcular el stock total para enviar alerta de umbral: %s", ex)
        return
    if total == MAX_STOCK_PER_SPIRAL:
        log.info("Stock total llegó al umbral configurado (%s). Enviando notificación por email.", MAX_STOCK_PER_SPIRAL)
        ok = notify_stock_threshold(total, MAX_STOCK_PER_SPIRAL, VENDING_CODE)
        if not ok:
            log.warning("No se pudo enviar el email de umbral de stock (revisar SMTP y NOTIFICATION_EMAILS en .env)")


# ==============================
# DISPENSAR POR CÓDIGO
# ==============================
def dispensar_por_codigo(codigo):
    stock_actual = dict(STOCK)
    if get_firestore and get_config_stock:
        try:
            db = get_firestore()
            stock_actual = get_config_stock(db)
            STOCK.update(stock_actual)
        except Exception as ex:
            log.warning(f"No se sincronizó stock: {ex}")

    # Notificaciones por correo: espirales en 0
    if _email_notifier_available and notify_espirales_sin_stock and notify_vending_sin_stock:
        espirales_en_0 = [e for e in ESPIRAL_ORDER if stock_actual.get(e, 0) <= 0]
        if len(espirales_en_0) == len(ESPIRAL_ORDER):
            notify_vending_sin_stock(VENDING_CODE)
        elif espirales_en_0:
            notify_espirales_sin_stock(espirales_en_0, VENDING_CODE)

    for i, espiral_id in enumerate(ESPIRAL_ORDER):
        cantidad = stock_actual.get(espiral_id, 0)
        if cantidad > 0:
            log.info(f"Intentando dispensar desde {espiral_id} (stock: {cantidad})")
            activar_relay(i)

            if esperar_deteccion():
                nuevo_stock = cantidad - 1
                STOCK[espiral_id] = nuevo_stock

                if get_firestore and update_config_stock and registrar_evento_history:
                    try:
                        db = get_firestore()
                        update_config_stock(db, espiral_id, nuevo_stock)
                        registrar_evento_history(db, codigo, 1, VENDING_CODE)
                        log.info(f"Dispensado OK - stock {espiral_id}: {cantidad} → {nuevo_stock}")
                    except Exception as ex:
                        log.exception("Error actualizando Firestore/history")

                # Después de dispensar y actualizar el stock, verificar si se alcanzó el umbral total
                _check_stock_threshold_and_notify()
                return True, None
            else:
                log.warning(f"No se detectó caída en {espiral_id} → posible atasco")

    return False, "No hay stock o no se detectó caída del producto"

# ==============================
# PRUEBA DE ESPIRAL
# ==============================
def ejecutar_prueba_espiral(idx):
    espiral_id = f"espiral{idx + 1}"
    if not all([get_firestore, update_config_stock, registrar_evento_history]):
        activar_relay(idx)
        _mostrar_alert_firestore("Prueba", "Prueba ejecutada (sin Firestore)")
        return

    try:
        db = get_firestore()
        stock = get_config_stock(db)
        cantidad = stock.get(espiral_id, 0)
        if cantidad <= 0:
            if _email_notifier_available and notify_espiral_cero_stock:
                log.info("Enviando notificación por email: %s sin stock", espiral_id)
                ok = notify_espiral_cero_stock(espiral_id, VENDING_CODE)
                if not ok:
                    log.warning("No se pudo enviar el email (revisar SMTP y NOTIFICATION_EMAILS en .env)")
            else:
                log.warning("No se envía email: notificador no disponible o no configurado")
            _mostrar_alert_firestore("Prueba", "Sin stock en este espiral")
            return

        activar_relay(idx)
        log.info(f"Prueba {espiral_id}: esperando impacto...")

        if esperar_deteccion():
            nuevo_stock = cantidad - 1
            update_config_stock(db, espiral_id, nuevo_stock)
            registrar_evento_history(db, "PRUEBA", 1, VENDING_CODE)
            STOCK[espiral_id] = nuevo_stock

            # Si el espiral llegó a 0, se mantiene la alerta específica de espiral sin stock
            if nuevo_stock <= 0 and _email_notifier_available and notify_espiral_cero_stock:
                log.info("Espiral %s llegó a 0 stock; enviando notificación por email", espiral_id)
                ok = notify_espiral_cero_stock(espiral_id, VENDING_CODE)
                if not ok:
                    log.warning("No se pudo enviar el email (revisar SMTP y NOTIFICATION_EMAILS en .env)")

            # Además, verificar si el stock total alcanzó el umbral configurado
            _check_stock_threshold_and_notify()

            _mostrar_alert_firestore("Prueba", "OK - impacto detectado")
        else:
            _mostrar_alert_firestore("Prueba", "No se detectó impacto (revisar logs)")
    except Exception as ex:
        log.exception("Error en prueba de espiral")
        _mostrar_alert_firestore("Error", f"Fallo en prueba:\n{str(ex)}")

# ==============================
# LAYOUT BASE
# ==============================
def pantalla_layout(contenido):
    card = ft.Container(
        content=ft.Column(
            contenido,
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=20
        ),
        width=800,
        padding=15,
    )

    fondo = ft.Container(
        content=ft.Image(
            src=FONDO_IMG,
            fit=ft.BoxFit.COVER,
            expand=True,
        ),
        expand=True,
        alignment=ft.Alignment(0, 0),
    )

    contenido_centrado = ft.Container(
        content=card,
        alignment=ft.Alignment(0, -0.1),
        expand=True,
    )

    page.controls.clear()
    page.add(ft.Stack([fondo, contenido_centrado], expand=True))
    page.update()

# ==============================
# ALERTAS
# ==============================
def _cerrar_dialogo_firestore(e):
    global _alert_firestore
    if _alert_firestore:
        _alert_firestore.open = False
        page.update()

def _mostrar_alert_firestore(titulo, contenido, on_ok=None):
    global _alert_firestore
    if _alert_firestore and _alert_firestore in page.overlay:
        page.overlay.remove(_alert_firestore)

    def _al_cerrar(e):
        _cerrar_dialogo_firestore(e)
        if on_ok:
            on_ok()

    content = contenido if isinstance(contenido, ft.Control) else ft.Text(str(contenido))
    _alert_firestore = ft.AlertDialog(
        title=ft.Text(titulo),
        content=content,
        actions=[ft.TextButton("OK", on_click=_al_cerrar)],
    )
    page.overlay.append(_alert_firestore)
    _alert_firestore.open = True
    page.update()

# ==============================
# TECLADO FIJO
# ==============================
def crear_teclado_onscreen(textfield: ft.TextField):

    def tecla_handler(letra):
        def fn(e):
            textfield.value = (textfield.value or "") + letra
            textfield.focus()
            page.update()
            bip_sound.play()
        return fn

    def borrar(e):
        if textfield.value:
            textfield.value = textfield.value[:-1]
        textfield.focus()
        page.update()
        bip_sound.play()

    def enter(e):
        validar_codigo(None)

    teclas = [
        "1234567890",
        "QWERTYUIOP",
        "ASDFGHJKL←",
        "ZXCVBNM"
    ]

    filas = []
    for linea in teclas:
        fila_btns = []
        for c in linea:
            if c == "←":
                btn = ft.ElevatedButton(
                    "Borrar",
                    width=120,
                    height=50,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.BLUE_GREY_700,
                        color="white",
                        text_style=ft.TextStyle(size=17, weight=ft.FontWeight.BOLD),
                        shape=ft.RoundedRectangleBorder(radius=10)
                    ),
                    on_click=borrar
                )
            elif c == "✓":
                btn = ft.ElevatedButton(
                    "✓",
                    width=65,
                    height=55,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.GREEN_700,
                        color="white",
                        text_style=ft.TextStyle(size=26, weight=ft.FontWeight.BOLD),
                        shape=ft.RoundedRectangleBorder(radius=14)
                    ),
                    on_click=enter
                )
            else:
                btn = ft.ElevatedButton(
                    c,
                    width=65,
                    height=50,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.BLUE_GREY_800,
                        color="white",
                        text_style=ft.TextStyle(size=26, weight=ft.FontWeight.BOLD),
                        shape=ft.RoundedRectangleBorder(radius=14)
                    ),
                    on_click=tecla_handler(c)
                )
            fila_btns.append(btn)

        filas.append(ft.Row(
            fila_btns,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=6
        ))

    teclado = ft.Column(
        filas,
        spacing=6,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER
    )

    contenedor_teclado = ft.Container(
        content=teclado,
        bgcolor=ft.Colors.with_opacity(0.92, ft.Colors.BLUE_GREY_900),
        padding=ft.padding.only(left=8, top=12, right=8, bottom=16),
        border_radius=16,
        alignment=ft.Alignment(0, 0),
        expand=True,
        width=float("inf"),
    )

    return contenedor_teclado

# ==============================
# PANTALLA PRINCIPAL
# ==============================
codigo_input = None

def validar_codigo(e):
    global codigo_input
    codigo = (codigo_input.value or "").strip()
    if not codigo:
        return

    if codigo.lower() == CODIGO_ADMIN.lower():
        pantalla_admin()
        return

    codigo_lower = codigo.lower().strip()

    pantalla_layout([
        ft.ProgressRing(),
        ft.Text("Validando código...", size=26, weight=ft.FontWeight.BOLD, color="white")
    ])

    async def async_proceso():
        valido, error = await asyncio.to_thread(validar_codigo_api, codigo_lower)
        if not valido:
            _mostrar_alert_firestore("Error", error or "Código inválido", on_ok=pantalla_principal)
            return

        pantalla_layout([
            ft.ProgressRing(),
            ft.Text("Entregando premio...", size=24, weight=ft.FontWeight.BOLD, color="#1F3A93")
        ])

        exito, msg = await asyncio.to_thread(dispensar_por_codigo, codigo_lower)
        if exito:
            await asyncio.to_thread(redimir_codigo_api, codigo_lower, VENDING_CODE)
            _mostrar_alert_firestore("¡Listo!", "¡Muchas gracias!", on_ok=pantalla_principal)
        else:
            _mostrar_alert_firestore("Error", msg or "No se pudo dispensar", on_ok=pantalla_principal)

    page.run_task(async_proceso)

def pantalla_principal():
    global codigo_input

    codigo_input = ft.TextField(
        width=420,
        height=68,
        text_size=30,
        text_style=ft.TextStyle(weight=ft.FontWeight.W_800),
        text_align=ft.TextAlign.CENTER,
        color="white",
        border_color=ft.Colors.WHITE70,
        cursor_color="white",
        bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.WHITE),
        hint_text="Código aquí...",
        hint_style=ft.TextStyle(color=ft.Colors.WHITE54, size=24),
        border_width=2,
        focused_border_width=3,
        focused_border_color=ft.Colors.CYAN_400,
        autofocus=True,
    )

    teclado = crear_teclado_onscreen(codigo_input)

    pantalla_layout([
        ft.Text("INGRESÁ TU CÓDIGO", size=42, weight=ft.FontWeight.W_900, color="white"),
        codigo_input,
        ft.Container(height=16),
        teclado,
        ft.Container(height=24),
        ft.ElevatedButton(
            "VALIDAR",
            width=280,
            height=64,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                shape=ft.RoundedRectangleBorder(radius=36),
                text_style=ft.TextStyle(size=26, weight=ft.FontWeight.BOLD)
            ),
            on_click=validar_codigo
        ),
    ])

# ==============================
# PANEL ADMINISTRACIÓN - RESTAURADO COMPLETO
# ==============================
def probar_conexion_firestore(e):
    if not get_firestore:
        _mostrar_alert_firestore("Firestore", "No configurado")
        return
    try:
        db = get_firestore()
        get_config_stock(db)
        _mostrar_alert_firestore("Firestore", "Conexión OK")
    except Exception as ex:
        _mostrar_alert_firestore("Firestore", f"Error: {ex}")

def cerrar_para_config_wifi(e):
    page.window.minimized = True
    page.update()

def probar_email(e):
    """Envía un email de prueba (alerta espiral sin stock) y muestra el resultado."""
    if not _email_notifier_available or not notify_espiral_cero_stock:
        _mostrar_alert_firestore("Probar email", "Notificador de email no disponible.\nRevisá que email_notifier esté instalado y que no haya fallado al importar.")
        return
    log.info("Enviando email de prueba...")
    ok = notify_espiral_cero_stock("espiral1", VENDING_CODE)
    if ok:
        _mostrar_alert_firestore("Probar email", "Email de prueba enviado correctamente.")
    else:
        _mostrar_alert_firestore("Probar email", "No se pudo enviar el email.\nRevisá SMTP_* y NOTIFICATION_EMAILS en .env")

def _btn_admin(text, bgcolor, on_click, width=280, height=46):
    return ft.ElevatedButton(
        text,
        width=width,
        height=height,
        style=ft.ButtonStyle(
            bgcolor=bgcolor,
            color="white",
            text_style=ft.TextStyle(size=18, weight=ft.FontWeight.BOLD),
        ),
        on_click=on_click,
    )

def pantalla_admin():
    col1 = ft.Column(
        [
            _btn_admin("Probar espirales", ft.Colors.BLUE_700, lambda e: pantalla_test_espirales()),
            ft.Container(height=18),
            _btn_admin("Probar Firestore", ft.Colors.INDIGO_700, probar_conexion_firestore),
            ft.Container(height=18),
            _btn_admin("Probar email", ft.Colors.INDIGO_600, probar_email),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=0,
    )
    col2 = ft.Column(
        [
            _btn_admin("Ajustar stock", ft.Colors.TEAL_700, lambda e: pantalla_stock()),
            ft.Container(height=18),
            _btn_admin("Reporte", ft.Colors.PURPLE_700, lambda e: pantalla_reportes()),
            ft.Container(height=18),
            _btn_admin("Configurar WiFi", ft.Colors.ORANGE_700, cerrar_para_config_wifi),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=0,
    )
    pantalla_layout([
        ft.Text("ADMINISTRACIÓN", size=36, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        ft.Container(height=16),
        ft.Row(
            [col1, col2],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=24,
        ),
        ft.Container(height=24),
        ft.ElevatedButton(
            "Volver",
            width=220,
            height=35,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD),
            ),
            on_click=lambda e: pantalla_principal(),
        ),
    ])

def pantalla_test_espirales():
    botones = []
    for i in range(4):
        btn = ft.ElevatedButton(
            f"Probar Espiral {i+1}",
            width=300,
            height=40,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_GREY_600,
                color="white",
                text_style=ft.TextStyle(size=18)
            ),
            on_click=lambda e, idx=i: ejecutar_prueba_espiral(idx)
        )
        botones.append(btn)
        botones.append(ft.Container(height=10))

    pantalla_layout([
        ft.Text("Probar Espirales", size=32, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        *botones,
        ft.Container(height=20),
        ft.ElevatedButton(
            "Volver",
            width=220,
            height=35,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD)
            ),
            on_click=lambda e: pantalla_admin()
        )
    ])


def pantalla_reportes():
    """Pantalla de reporte: historial (history) con filtros fecha desde / hasta. Por defecto muestra el día actual."""
    hoy = date.today().isoformat()
    tf_desde = ft.TextField(
        label="Desde",
        value=hoy,
        width=140,
        height=50,
        text_size=16,
        bgcolor="white",
        border_radius=6,
        hint_text="AAAA-MM-DD",
    )
    tf_hasta = ft.TextField(
        label="Hasta",
        value=hoy,
        width=140,
        height=50,
        text_size=16,
        bgcolor="white",
        border_radius=6,
        hint_text="AAAA-MM-DD",
    )
    report_list_ref = ft.Ref[ft.Column]()

    def parse_fecha(s):
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return date.today()

    def al_buscar(e):
        if not get_firestore or not get_history_by_date_range:
            _mostrar_alert_firestore("Reporte", "Firestore no disponible")
            return
        d_from = parse_fecha(tf_desde.value or hoy)
        d_to = parse_fecha(tf_hasta.value or hoy)
        if d_from > d_to:
            _mostrar_alert_firestore("Reporte", "La fecha 'Desde' debe ser menor o igual que 'Hasta'.")
            return
        # Mostrar cargando en la lista
        if report_list_ref.current:
            report_list_ref.current.controls = [
                ft.ProgressRing(),
                ft.Text("Cargando historial...", color="white", size=16),
            ]
            report_list_ref.current.update()
            page.update()

        async def cargar():
            try:
                db = get_firestore()
                registros = await asyncio.to_thread(get_history_by_date_range, db, d_from, d_to)
            except Exception as ex:
                log.exception("Error cargando historial: %s", ex)
                if report_list_ref.current:
                    report_list_ref.current.controls = [
                        ft.Text(f"Error: {ex}", color=ft.Colors.RED_300, size=14, no_wrap=False),
                    ]
                    report_list_ref.current.update()
                    page.update()
                return
            # Cabecera
            filas = [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Text("Fecha", size=14, weight=ft.FontWeight.BOLD, color="white", width=180),
                            ft.Text("Tipo", size=14, weight=ft.FontWeight.BOLD, color="white", width=80),
                            ft.Text("Código", size=14, weight=ft.FontWeight.BOLD, color="white", width=120),
                            ft.Text("Cant.", size=14, weight=ft.FontWeight.BOLD, color="white", width=50),
                        ],
                        spacing=8,
                    ),
                    bgcolor=ft.Colors.with_opacity(0.3, ft.Colors.WHITE),
                    padding=6,
                    border_radius=6,
                ),
            ]
            for r in registros:
                fecha_short = (r.get("fecha") or "")[:19].replace("T", " ") if r.get("fecha") else ""
                filas.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Text(fecha_short, size=13, color="white", width=180, no_wrap=True),
                                ft.Text(r.get("tipo", ""), size=13, color="white", width=80, no_wrap=True),
                                ft.Text(str(r.get("codigo", "")), size=13, color="white", width=120, no_wrap=True),
                                ft.Text(str(r.get("cantidad", 0)), size=13, color="white", width=50),
                            ],
                            spacing=8,
                        ),
                        padding=4,
                        border_radius=4,
                        bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.WHITE),
                    )
                )
            if not registros:
                filas.append(ft.Text("No hay registros en el rango seleccionado.", color=ft.Colors.WHITE70, size=14))
            if report_list_ref.current:
                report_list_ref.current.controls = filas
                report_list_ref.current.update()
                page.update()

        page.run_task(cargar)

    # Contenedor scrolleable para la lista del reporte
    lista_reportes = ft.Column(
        ref=report_list_ref,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        spacing=4,
        controls=[
            ft.Text("Seleccioná fechas y pulsá Buscar.", color="white", size=14),
        ],
    )
    contenedor_lista = ft.Container(
        content=lista_reportes,
        height=320,
        border=ft.border.all(1, ft.Colors.WHITE24),
        border_radius=8,
        padding=8,
        bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.BLACK),
    )

    pantalla_layout([
        ft.Text("Reporte - Historial", size=32, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        ft.Row(
            [tf_desde, tf_hasta],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=16,
        ),
        ft.ElevatedButton(
            "Buscar",
            width=160,
            height=40,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.PURPLE_600,
                color="white",
                text_style=ft.TextStyle(size=18, weight=ft.FontWeight.BOLD),
            ),
            on_click=al_buscar,
        ),
        ft.Container(height=8),
        contenedor_lista,
        ft.Container(height=12),
        ft.ElevatedButton(
            "Volver",
            width=220,
            height=35,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD),
            ),
            on_click=lambda e: pantalla_admin(),
        ),
    ])
    # Cargar por defecto el día actual
    al_buscar(None)


def pantalla_stock():
    global STOCK
    if get_firestore and get_config_stock:
        try:
            STOCK = get_config_stock(get_firestore())
        except:
            pass

    inputs = {}
    filas = []

    for i in range(4):
        key = f"espiral{i+1}"

        tf = ft.TextField(
            value=str(STOCK.get(key, 0)),
            width=80,
            height=50,
            text_align=ft.TextAlign.CENTER,
            text_size=20,
            bgcolor="white",
            border_radius=6
        )

        inputs[key] = tf

        def crear_sumar(tf):
            def sumar(e):
                try:
                    v = int(tf.value)
                except:
                    v = 0
                tf.value = str(v + 1)
                tf.update()
            return sumar

        def crear_restar(tf):
            def restar(e):
                try:
                    v = int(tf.value)
                except:
                    v = 0
                if v > 0:
                    tf.value = str(v - 1)
                    tf.update()
            return restar

        fila = ft.Row(
            [
                ft.Text(f"Espiral {i+1}", size=20, width=140, color="white"),

                tf,

                ft.IconButton(
                    icon=ft.Icons.REMOVE,
                    icon_color="white",
                    bgcolor=ft.Colors.RED_400,
                    width=40,
                    height=40,
                    on_click=crear_restar(tf)
                ),

                ft.IconButton(
                    icon=ft.Icons.ADD,
                    icon_color="white",
                    bgcolor=ft.Colors.GREEN_400,
                    width=40,
                    height=40,
                    on_click=crear_sumar(tf, inputs)
                ),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8
        )

        filas.append(fila)
        filas.append(ft.Container(height=1))

    def guardar(e):
        # Tomar los valores ingresados, normalizarlos y guardar sin límite, pero chequeando el umbral para notificación
        for key, tf in inputs.items():
            try:
                v = int(tf.value or 0)
            except (ValueError, TypeError):
                v = 0
            STOCK[key] = max(0, v)

        if get_firestore and update_config_stock:
            db = get_firestore()
            for k, v in STOCK.items():
                update_config_stock(db, k, v)

        # Luego de guardar cambios manuales de stock, verificar el umbral total
        _check_stock_threshold_and_notify()

        _mostrar_alert_firestore(
            "Stock",
            "Guardado correctamente",
            on_ok=pantalla_admin
        )

    pantalla_layout([
        ft.Text(
            "Ajustar Stock",
            size=32,
            weight=ft.FontWeight.BOLD,
            color="#FFFFFF"
        ),

        *filas,

        ft.Container(height=10),

        ft.ElevatedButton(
            "Guardar cambios",
            width=220,
            height=35,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD)
            ),
            on_click=guardar
        ),

        ft.Container(height=5),

        ft.ElevatedButton(
            "Volver",
            width=220,
            height=35,
            style=ft.ButtonStyle(
                bgcolor="white",
                color="#1F3A93",
                text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD)
            ),
            on_click=lambda e: pantalla_principal()
        ),
    ])

# ==============================
# MAIN
# ==============================
def main(p: ft.Page):
    global page
    page = p
    page.padding = 0
    page.spacing = 0
    page.margin = 0
    page.window.frameless = True
    page.window.full_screen = True
    page.window_resizable = False
    page.window_maximizable = False
    page.window_minimizable = False

    page.title = "Vending - NexoIot"
    page.bgcolor = ft.Colors.BLUE_GREY_900
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    try:
        log.info("Inicializando ADXL345...")
        i2c = busio.I2C(board.SCL, board.SDA)
        global accel
        accel = adafruit_adxl34x.ADXL345(i2c)
        accel.range = adafruit_adxl34x.Range.RANGE_16_G
        accel.data_rate = adafruit_adxl34x.DataRate.RATE_100_HZ
        accel.enable_tap_detection(tap_count=1, threshold=20, duration=20, latency=50, window=255)
        log.info("ADXL345 inicializado")
    except Exception as ex:
        log.error("Fallo ADXL345", exc_info=True)
        accel = None

    pantalla_principal()

if __name__ == "__main__":
    try:
        ft.app(target=main)
    finally:
        GPIO.cleanup()
        pygame.mixer.quit()
        if accel:
            try:
                accel.enable_tap_detection(False)
            except:
                pass
