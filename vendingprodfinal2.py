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

MODO_PRUEBAS = False     # cambiar a False cuando quieras volver a la API real

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
        ensure_vending_config,
        registrar_evento_history,
        get_history_by_date_range,
    )
    _firestore_import_error = None
except Exception as _firestore_import_error:
    get_firestore = get_config_stock = update_config_stock = ensure_vending_config = registrar_evento_history = get_history_by_date_range = None

try:
    from email_notifier import (
        notify_vending_sin_stock,
        notify_stock_threshold,
        notify_smtp_test,
    )
    _email_notifier_available = True
    _email_notifier_error = None
except Exception as _email_notifier_err:
    notify_vending_sin_stock = notify_stock_threshold = notify_smtp_test = None
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
    if MODO_PRUEBAS:
        log.info(f"[PRUEBAS] Código aceptado sin consultar API: {codigo}")
        return True, None
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
    if MODO_PRUEBAS:
        log.info(f"[PRUEBAS] Redención simulada OK para {codigo}")
        return True, None
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
    total = 0
    for esp in ESPIRAL_ORDER:
        try:
            total += int(STOCK.get(esp, 0) or 0)
        except (ValueError, TypeError):
            pass
    return total

def _check_stock_threshold_and_notify():
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
            stock_actual = get_config_stock(db, VENDING_CODE)
            STOCK.update(stock_actual)
        except Exception as ex:
            log.warning(f"No se sincronizó stock: {ex}")

    if _email_notifier_available and notify_vending_sin_stock:
        espirales_en_0 = [e for e in ESPIRAL_ORDER if stock_actual.get(e, 0) <= 0]
        if len(espirales_en_0) == len(ESPIRAL_ORDER):
            notify_vending_sin_stock(VENDING_CODE)

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
                        update_config_stock(db, espiral_id, nuevo_stock, VENDING_CODE)
                        registrar_evento_history(db, codigo, 1, VENDING_CODE)
                        log.info(f"Dispensado OK - stock {espiral_id}: {cantidad} → {nuevo_stock}")
                    except Exception as ex:
                        log.exception("Error actualizando Firestore/history")

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
        stock = get_config_stock(db, VENDING_CODE)
        cantidad = stock.get(espiral_id, 0)
        if cantidad <= 0:
            log.info("Prueba %s: sin stock", espiral_id)
            _mostrar_alert_firestore("Prueba", "Sin stock en este espiral")
            return

        activar_relay(idx)
        log.info(f"Prueba {espiral_id}: esperando impacto...")

        if esperar_deteccion():
            nuevo_stock = cantidad - 1
            update_config_stock(db, espiral_id, nuevo_stock, VENDING_CODE)
            registrar_evento_history(db, "PRUEBA", 1, VENDING_CODE)
            STOCK[espiral_id] = nuevo_stock
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

def _mostrar_alert_firestore(
    tipo: str,
    mensaje: str,
    on_ok=None,
    detalles=None
):
    global _alert_firestore

    if _alert_firestore and _alert_firestore in page.overlay:
        page.overlay.remove(_alert_firestore)

    configuracion = {
        "exito": {
            "titulo": "¡Listo!",
            "color_titulo": ft.Colors.GREEN_700,
            "bgcolor_card": ft.Colors.GREEN_50,
            "color_boton": ft.Colors.GREEN_600,
        },
        "error": {
            "titulo": "¡Ups!",
            "color_titulo": ft.Colors.RED_700,
            "bgcolor_card": ft.Colors.RED_50,
            "color_boton": ft.Colors.RED_600,
        },
        "info": {
            "titulo": "Atención",
            "color_titulo": ft.Colors.BLUE_700,
            "bgcolor_card": ft.Colors.BLUE_50,
            "color_boton": ft.Colors.BLUE_600,
        },
        "advertencia": {
            "titulo": "Atención",
            "color_titulo": ft.Colors.ORANGE_800,
            "bgcolor_card": ft.Colors.ORANGE_50,
            "color_boton": ft.Colors.ORANGE_700,
        }
    }

    config = configuracion.get(tipo.lower(), configuracion["info"])

    contenido = ft.Column(
        [
            ft.Text(
                config["titulo"],
                size=32,
                weight=ft.FontWeight.BOLD,
                color=config["color_titulo"],
                text_align=ft.TextAlign.CENTER,
            ),
            ft.Container(height=8),
            ft.Text(
                mensaje,
                size=32,
                color=ft.Colors.BLUE_GREY_800,
                text_align=ft.TextAlign.CENTER,
                weight=ft.FontWeight.W_500,
            ),
        ],
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=16,
    )

    if detalles:
        contenido.controls.append(
            ft.Container(height=8),
            ft.Text(
                detalles,
                size=14,
                color=ft.Colors.BLUE_GREY_600,
                text_align=ft.TextAlign.CENTER,
                italic=True,
            )
        )

    boton_ok = ft.ElevatedButton(
        "Entendido",
        width=240,
        height=56,
        style=ft.ButtonStyle(
            bgcolor=config["color_boton"],
            color=ft.Colors.WHITE,
            text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD),
            shape=ft.RoundedRectangleBorder(radius=16),
            elevation=4,
        ),
        on_click=lambda e: _al_cerrar(e),
    )

    def _al_cerrar(e):
        if _alert_firestore:
            _alert_firestore.open = False
            page.update()
        if on_ok:
            on_ok()

    alert = ft.AlertDialog(
        modal=True,
        content_padding=ft.padding.symmetric(horizontal=40, vertical=20),
        shape=ft.RoundedRectangleBorder(radius=28),
        bgcolor=ft.Colors.WHITE,
        inset_padding=ft.padding.symmetric(horizontal=40, vertical=60),
        content=ft.Container(
            content=contenido,
            padding=ft.padding.only(top=16, bottom=16, left=24, right=24),
            bgcolor=config["bgcolor_card"],
            border_radius=20,
            width=600,
            alignment=ft.Alignment(0, 0),
        ),
        actions=[boton_ok],
        actions_alignment=ft.MainAxisAlignment.CENTER,
        actions_padding=ft.padding.only(bottom=20),
    )

    _alert_firestore = alert
    page.overlay.append(alert)
    alert.open = True
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
# VALIDACIÓN DE CÓDIGO (VERSIÓN MEJORADA)
# ==============================
codigo_input = None

async def async_proceso():
    codigo = (codigo_input.value or "").strip()
    if not codigo:
        return

    codigo_lower = codigo.lower()

    if codigo_lower == "espirales1234":
        pantalla_test_espirales()
        return
    if codigo_lower == CODIGO_ADMIN.lower():
        pantalla_admin()
        return

    # 1. Validando código en la API
    pantalla_layout([
        ft.ProgressRing(color=ft.Colors.WHITE, stroke_width=8),
        ft.Text("Validando código...", size=26, weight=ft.FontWeight.BOLD, color="white")
    ])

    valido, error = await asyncio.to_thread(validar_codigo_api, codigo)
    if not valido:
        _mostrar_alert_firestore("Error", error or "Código inválido o ya usado", 
                               on_ok=pantalla_principal)
        return

    # 2. Verificar stock ANTES de redimir
    pantalla_layout([
        ft.ProgressRing(color=ft.Colors.WHITE, stroke_width=8),
        ft.Text("Verificando stock...", size=26, weight=ft.FontWeight.BOLD, color="white")
    ])

    stock_actual = dict(STOCK)
    if get_firestore and get_config_stock:
        try:
            db = get_firestore()
            stock_actual = get_config_stock(db, VENDING_CODE)
            STOCK.update(stock_actual)
        except Exception as ex:
            log.warning(f"Error al obtener stock: {ex}")

    # Comprobar si hay stock disponible
    tiene_stock = any(int(stock_actual.get(esp, 0)) > 0 for esp in ESPIRAL_ORDER)

    if not tiene_stock:
        log.warning(f"Código válido pero sin stock en la vending: {codigo}")
        if _email_notifier_available and notify_vending_sin_stock:
            notify_vending_sin_stock(VENDING_CODE)

        _mostrar_alert_firestore(
            "advertencia",
            "¡Lo sentimos!\nEn este momento no tenemos stock disponible.\nPor favor intentá más tarde.",
            on_ok=pantalla_principal
        )
        return

    # 3. Redimir código en la API (solo si hay stock)
    pantalla_layout([
        ft.ProgressRing(color=ft.Colors.WHITE, stroke_width=8),
        ft.Text("Redimiendo código...", size=24, weight=ft.FontWeight.BOLD, color="#FFFFFF")
    ])

    redimido, error_redencion = await asyncio.to_thread(redimir_codigo_api, codigo, VENDING_CODE)

    if not redimido:
        log.error(f"Error al redimir código {codigo}: {error_redencion or 'Error desconocido'}")
        _mostrar_alert_firestore(
            "Error", 
            "Error al procesar el código. Por favor contactá soporte en ¡Appa!",
            on_ok=pantalla_principal
        )
        return

    # 4. Dispensar el producto
    pantalla_layout([
        ft.ProgressRing(color=ft.Colors.WHITE, stroke_width=8),
        ft.Text("Entregando premio...", size=24, weight=ft.FontWeight.BOLD, color="#FFFFFF")
    ])

    exito, msg = await asyncio.to_thread(dispensar_por_codigo, codigo)

    if exito:
        _mostrar_alert_firestore("¡Listo!", "Premio canjeado con éxito.", on_ok=pantalla_principal)
    else:
        _mostrar_alert_firestore("Error", msg or "No se pudo dispensar el producto.", on_ok=pantalla_principal)


def validar_codigo(e):
    page.run_task(async_proceso)


# ==============================
# PANTALLA PRINCIPAL
# ==============================
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
# PANEL ADMINISTRACIÓN (sin cambios)
# ==============================
def probar_conexion_firestore(e):
    if not get_firestore:
        _mostrar_alert_firestore("Error", "No configurado")
        return
    try:
        db = get_firestore()
        get_config_stock(db, VENDING_CODE)
        _mostrar_alert_firestore("Firestore", "Conexión OK")
    except Exception as ex:
        _mostrar_alert_firestore("Firestore", f"Error: {ex}")

def cerrar_para_config_wifi(e):
    page.window.minimized = True
    page.update()

def probar_email(e):
    if not _email_notifier_available or not notify_smtp_test:
        _mostrar_alert_firestore("Probar email", "Notificador de email no disponible.")
        return
    log.info("Enviando email de prueba SMTP...")
    ok = notify_smtp_test(VENDING_CODE)
    if ok:
        _mostrar_alert_firestore("Probar email", "Email de prueba enviado correctamente.")
    else:
        _mostrar_alert_firestore("Probar email", "No se pudo enviar el email.")

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
    col1 = ft.Column([
        _btn_admin("Probar Firestore", ft.Colors.INDIGO_700, probar_conexion_firestore),
        ft.Container(height=18),
        _btn_admin("Probar email", ft.Colors.INDIGO_600, probar_email),
        ft.Container(height=18),
        _btn_admin("Ajustar stock", ft.Colors.TEAL_700, lambda e: pantalla_stock()),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    col2 = ft.Column([
        _btn_admin("Reporte", ft.Colors.PURPLE_700, lambda e: pantalla_reportes()),
        ft.Container(height=18),
        _btn_admin("Configurar WiFi", ft.Colors.ORANGE_700, cerrar_para_config_wifi),
    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    pantalla_layout([
        ft.Text("ADMINISTRACIÓN", size=36, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        ft.Container(height=16),
        ft.Row([col1, col2], alignment=ft.MainAxisAlignment.CENTER, spacing=24),
        ft.Container(height=24),
        ft.ElevatedButton(
            "Volver",
            width=220,
            height=35,
            style=ft.ButtonStyle(bgcolor="white", color="#1F3A93", text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD)),
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
            style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE_GREY_600, color="white"),
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
            style=ft.ButtonStyle(bgcolor="white", color="#1F3A93", text_style=ft.TextStyle(size=20, weight=ft.FontWeight.BOLD)),
            on_click=lambda e: pantalla_admin()
        )
    ])

def pantalla_reportes():
    hoy = date.today().isoformat()
    tf_desde = ft.TextField(label="Desde", value=hoy, width=140, height=50, text_size=16, bgcolor="white")
    tf_hasta = ft.TextField(label="Hasta", value=hoy, width=140, height=50, text_size=16, bgcolor="white")
    report_list_ref = ft.Ref[ft.Column]()

    def parse_fecha(s):
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except:
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

        if report_list_ref.current:
            report_list_ref.current.controls = [ft.ProgressRing(), ft.Text("Cargando historial...", color="white")]
            report_list_ref.current.update()
            page.update()

        async def cargar():
            try:
                db = get_firestore()
                registros = await asyncio.to_thread(get_history_by_date_range, db, d_from, d_to, 500, VENDING_CODE)
            except Exception as ex:
                log.exception("Error cargando historial")
                if report_list_ref.current:
                    report_list_ref.current.controls = [ft.Text(f"Error: {ex}", color=ft.Colors.RED_300)]
                    report_list_ref.current.update()
                return

            filas = [ft.Container(content=ft.Row([ft.Text("Fecha", weight=ft.FontWeight.BOLD, width=180), 
                                                 ft.Text("Tipo", weight=ft.FontWeight.BOLD, width=80),
                                                 ft.Text("Código", weight=ft.FontWeight.BOLD, width=120),
                                                 ft.Text("Cant.", weight=ft.FontWeight.BOLD, width=50)], spacing=8),
                                 bgcolor=ft.Colors.with_opacity(0.3, ft.Colors.WHITE), padding=6)]

            for r in registros:
                fecha_short = (r.get("fecha") or "")[:19].replace("T", " ") if r.get("fecha") else ""
                filas.append(ft.Container(content=ft.Row([
                    ft.Text(fecha_short, width=180),
                    ft.Text(r.get("tipo", ""), width=80),
                    ft.Text(str(r.get("codigo", "")), width=120),
                    ft.Text(str(r.get("cantidad", 0)), width=50)
                ], spacing=8), padding=4, bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.WHITE)))

            if not registros:
                filas.append(ft.Text("No hay registros en el rango seleccionado.", color=ft.Colors.WHITE70))

            if report_list_ref.current:
                report_list_ref.current.controls = filas
                report_list_ref.current.update()
                page.update()

        page.run_task(cargar)

    lista_reportes = ft.Column(ref=report_list_ref, scroll=ft.ScrollMode.AUTO, expand=True)
    contenedor_lista = ft.Container(content=lista_reportes, height=320, border=ft.border.all(1, ft.Colors.WHITE24), 
                                    border_radius=8, padding=8, bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.BLACK))

    pantalla_layout([
        ft.Text("Reporte - Historial", size=32, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        ft.Row([tf_desde, tf_hasta], alignment=ft.MainAxisAlignment.CENTER, spacing=16),
        ft.ElevatedButton("Buscar", width=160, height=40, style=ft.ButtonStyle(bgcolor=ft.Colors.PURPLE_600, color="white"), on_click=al_buscar),
        ft.Container(height=8),
        contenedor_lista,
        ft.Container(height=12),
        ft.ElevatedButton("Volver", width=220, height=35, style=ft.ButtonStyle(bgcolor="white", color="#1F3A93"), on_click=lambda e: pantalla_admin()),
    ])
    al_buscar(None)

def pantalla_stock():
    global STOCK
    if get_firestore and get_config_stock:
        try:
            STOCK = get_config_stock(get_firestore(), VENDING_CODE)
        except:
            pass

    inputs = {}
    filas = []

    for i in range(4):
        key = f"espiral{i+1}"
        tf = ft.TextField(value=str(STOCK.get(key, 0)), width=80, height=50, text_align=ft.TextAlign.CENTER, text_size=20, bgcolor="white")

        inputs[key] = tf

        def crear_sumar(tf):
            def sumar(e):
                try: v = int(tf.value or 0)
                except: v = 0
                tf.value = str(v + 1)
                tf.update()
            return sumar

        def crear_restar(tf):
            def restar(e):
                try: v = int(tf.value or 0)
                except: v = 0
                if v > 0:
                    tf.value = str(v - 1)
                    tf.update()
            return restar

        fila = ft.Row([
            ft.Text(f"Espiral {i+1}", size=20, width=140, color="white"),
            tf,
            ft.IconButton(icon=ft.Icons.REMOVE, icon_color="white", bgcolor=ft.Colors.RED_400, on_click=crear_restar(tf)),
            ft.IconButton(icon=ft.Icons.ADD, icon_color="white", bgcolor=ft.Colors.GREEN_400, on_click=crear_sumar(tf)),
        ], alignment=ft.MainAxisAlignment.CENTER)

        filas.append(fila)
        filas.append(ft.Container(height=1))

    def guardar(e):
        for key, tf in inputs.items():
            try:
                v = int(tf.value or 0)
            except:
                v = 0
            STOCK[key] = max(0, v)

        if get_firestore and update_config_stock:
            db = get_firestore()
            for k, v in STOCK.items():
                update_config_stock(db, k, v, VENDING_CODE)

        _check_stock_threshold_and_notify()
        _mostrar_alert_firestore("Stock", "Guardado correctamente", on_ok=pantalla_admin)

    pantalla_layout([
        ft.Text("Ajustar Stock", size=32, weight=ft.FontWeight.BOLD, color="#FFFFFF"),
        *filas,
        ft.Container(height=10),
        ft.ElevatedButton("Guardar stock", width=220, height=35, style=ft.ButtonStyle(bgcolor="white", color="#1F3A93"), on_click=guardar),
        ft.Container(height=5),
        ft.ElevatedButton("Volver", width=220, height=35, style=ft.ButtonStyle(bgcolor="white", color="#1F3A93"), on_click=lambda e: pantalla_principal()),
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

    if get_firestore and ensure_vending_config:
        try:
            db = get_firestore()
            created = ensure_vending_config(db, VENDING_CODE)
            STOCK.update(get_config_stock(db, VENDING_CODE))
            if created:
                log.info("Se creó configuración inicial en Firestore")
        except Exception as ex:
            log.warning("No se pudo verificar config inicial: %s", ex)

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
