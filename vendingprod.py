import asyncio
import flet as ft
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import RPi.GPIO as GPIO

# Sensores y sonido
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
    )
    _firestore_import_error = None
except Exception as _firestore_import_error:
    get_firestore = get_config_stock = update_config_stock = registrar_evento_history = None

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
RELAY_PINS = [4, 6, 22, 26]  # Ajustar según tu conexión real
TIEMPO_GIRO = 1               # segundos que gira el espiral

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in RELAY_PINS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

# ==============================
# VARIABLES GLOBALES
# ==============================
STOCK = {}
CODIGO_ADMIN = os.getenv("CODIGO_ADMIN", "admin1234")

# API
URL_API = (os.getenv("url_api") or "").rstrip("/")
STORE_ID = os.getenv("storeId", "")
API_KEY = os.getenv("x-api-key", "")
VENDING_CODE = os.getenv("vendingCode", "")

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
        _ = accel.events['tap']  # limpiar eventos pendientes
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
                    try:
                        acc = accel.acceleration
                        g = (acc[0]**2 + acc[1]**2 + acc[2]**2)**0.5 / 9.81
                        log.info(f"Aceleración: X={acc[0]/9.81:.2f}g Y={acc[1]/9.81:.2f}g Z={acc[2]/9.81:.2f}g | Mag={g:.2f}g")
                    except:
                        pass

                    for _ in range(REPETICIONES):
                        bip_sound.play()
                        time.sleep(PAUSA_ENTRE)

                    last_detection_time = now
                    _ = accel.events['tap']  # limpiar
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
            _mostrar_alert_firestore("Prueba", "Sin stock en este espiral")
            return

        activar_relay(idx)
        log.info(f"Prueba {espiral_id}: esperando impacto...")

        if esperar_deteccion():
            nuevo_stock = cantidad - 1
            update_config_stock(db, espiral_id, nuevo_stock)
            registrar_evento_history(db, "PRUEBA", 1, VENDING_CODE)
            STOCK[espiral_id] = nuevo_stock
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
            alignment="center",
            horizontal_alignment="center",
            spacing=20
        ),
        width=500,
        padding=30,
        border_radius=20
    )

    fondo = ft.Container(
    content=ft.Image(
        src=FONDO_IMG,
        fit="cover",
        expand=True,
    ),
    expand=True,
    alignment=ft.alignment.Alignment(0, 0),
)
    contenido_centrado = ft.Container(
        content=card,
        alignment=ft.alignment.Alignment(0, 0),
        expand=True,
    )

    page.controls.clear()
    page.add(
        ft.Stack(
            controls=[fondo, contenido_centrado],
            expand=True,
        )
    )
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
# PANTALLA PRINCIPAL
# ==============================
def pantalla_principal():
    codigo_input = ft.TextField(
    width=320,
    text_align=ft.TextAlign.CENTER,
    color="white",
    border_color="white",
    cursor_color="white",
    bgcolor=ft.Colors.with_opacity(0.15, "white"),
)
    mensaje = ft.Text("", color="red")

    def validar_codigo(e):
        codigo = codigo_input.value.strip()
        if not codigo:
            mensaje.value = "Ingresá un código"
            page.update()
            return

        if codigo.lower() == CODIGO_ADMIN:
            pantalla_admin()
            return

        codigo_lower = codigo.lower().strip()

        pantalla_layout([
            ft.ProgressRing(),
            ft.Text("Validando código...", size=24, weight=ft.FontWeight.BOLD, color="#FFFFFF")
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

    pantalla_layout([
        ft.Text("INGRESÁ TU CÓDIGO", size=40, weight=ft.FontWeight.BOLD, color="white"),
        codigo_input,
        ft.ElevatedButton(
    "VALIDAR",
    width=220,
    height=50,
    style=ft.ButtonStyle(
        bgcolor="white",
        color="#1F3A93",
        shape=ft.RoundedRectangleBorder(radius=30)
    ),
    on_click=validar_codigo
),
        mensaje
    ])

# ==============================
# ADMIN
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

def pantalla_admin():
    pantalla_layout([
        ft.Text("ADMINISTRACIÓN", size=30, weight=ft.FontWeight.BOLD, color="#1F3A93"),
        ft.Button("Probar espirales", width=250, on_click=lambda e: pantalla_test_espirales()),
        ft.Button("Probar Firestore", width=250, on_click=probar_conexion_firestore),
        ft.Button("Ajustar stock", width=250, on_click=lambda e: pantalla_stock()),
        ft.Button("Configurar WiFi", width=250, on_click=lambda e: os.system("nm-connection-editor &")),
        ft.Divider(),
        ft.Button("Salir", width=250, on_click=lambda e: pantalla_principal())
    ])

def pantalla_test_espirales():
    botones = [
        ft.Button(f"Espiral {i+1}", width=200,
                  on_click=lambda e, idx=i: ejecutar_prueba_espiral(idx))
        for i in range(4)
    ]
    pantalla_layout([
        ft.Text("Probar Espirales", size=26, weight=ft.FontWeight.BOLD, color="#1F3A93"),
        *botones,
        ft.Button("Volver", on_click=lambda e: pantalla_admin())
    ])

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
        tf = ft.TextField(value=str(STOCK.get(key, 0)), width=100, text_align="center")
        inputs[key] = tf
        filas.append(ft.Row([ft.Text(f"Espiral {i+1}", width=120), tf], alignment=ft.MainAxisAlignment.CENTER))

    def guardar(e):
        for key, tf in inputs.items():
            try:
                STOCK[key] = int(tf.value)
            except:
                pass
        if get_firestore and update_config_stock:
            db = get_firestore()
            for k, v in STOCK.items():
                update_config_stock(db, k, v)
        _mostrar_alert_firestore("Stock", "Guardado correctamente", on_ok=pantalla_admin)

    pantalla_layout([
        ft.Text("Ajustar Stock", size=26, weight=ft.FontWeight.BOLD, color="#1F3A93"),
        *filas,
        ft.Button("Guardar cambios", on_click=guardar),
        ft.Button("Volver", on_click=lambda e: pantalla_admin())
    ])

# ==============================
# MAIN - FULLSCREEN SIN BARRAS
# ==============================
def main(p: ft.Page):
    global page, accel
    page = p
    page.padding = 0
    page.spacing = 0
    page.margin = 0
    page.window_width = 1920 # O la resolución nativa de tu pantalla
    # ── Configuración FULLSCREEN sin barra de título ni bordes ──
    page.window.frameless = True           # Elimina completamente la barra de título
    page.window.full_screen = True         # Modo fullscreen nativo del sistema operativo
    page.window_resizable = False
    page.window_maximizable = False
    page.window_minimizable = False
    # page.window_always_on_top = True     # descomentar si quieres que esté SIEMPRE encima

    # Estas líneas suelen ser redundantes cuando usas full_screen + frameless,
    # pero en algunos casos de Raspberry Pi / ciertos drivers ayudan:
    # page.window_width = 1920
    # page.window_height = 1080
    # page.window_left = 0
    # page.window_top = 0

    page.title = "Vending - NexoIot"
    page.bgcolor = "#EAF6FF"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    # Inicializar ADXL345
    try:
        log.info("Inicializando ADXL345...")
        i2c = busio.I2C(board.SCL, board.SDA)
        accel = adafruit_adxl34x.ADXL345(i2c)
        accel.range = adafruit_adxl34x.Range.RANGE_16_G
        accel.data_rate = adafruit_adxl34x.DataRate.RATE_100_HZ
        accel.enable_tap_detection(
            tap_count=1,
            threshold=20,
            duration=20,
            latency=50,
            window=255
        )
        log.info("ADXL345 inicializado - detección de tap activada")
    except Exception as ex:
        log.error("Fallo al inicializar ADXL345", exc_info=True)
        accel = None

    pantalla_principal()

# ==============================
# EJECUCIÓN
# ==============================
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
