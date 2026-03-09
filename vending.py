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
import pygame

# Imports para ADXL345 (I2C - pines 2 y 3)
import board
import busio
import adafruit_adxl34x

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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(_log_filename, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ==============================
# CONFIGURACIÓN GPIO (solo relés)
# ==============================
RELAY_PINS = [26, 6, 22, 4]
TIEMPO_GIRO = 1.0

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
FONDO_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "fondo.jpg")

page = None
_alert_firestore = None
accel = None  # Se inicializa en main()

ESPIRAL_ORDER = ["espiral1", "espiral2", "espiral3", "espiral4"]

DEBOUNCE_SEC = 1.2
last_detection_time = 0

# ==============================
# SONIDO
# ==============================
FRECUENCIA_BIP   = 1200
DURACION_BIP     = 0.18
REPETICIONES     = 2
PAUSA_ENTRE      = 0.07
VOLUMEN_PYGAME   = 1.0

pygame.mixer.pre_init(frequency=44100, size=-16, channels=1, buffer=512)
pygame.mixer.init()

# Generar onda cuadrada correctamente (signed 16-bit, little-endian)
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

# Prueba de sonido al inicio
log.info("Prueba de sonido inicial (debería sonar un bip corto)...")
bip_sound.play()
time.sleep(0.8)

# ==============================
# DETECCIÓN DE IMPACTO (ADXL345)
# ==============================
def esperar_deteccion():
    global last_detection_time
    if accel is None:
        log.warning("ADXL345 no disponible → detección desactivada")
        time.sleep(TIEMPO_GIRO + 1)
        return False

    # Limpiar eventos pendientes
    try:
        _ = accel.events['tap']
    except:
        pass

    inicio = time.time()
    log.info("Esperando impacto ADXL345... (máx 7 segundos)")

    while time.time() - inicio < 7:
        try:
            if accel.events['tap']:
                now = time.time()
                if now - last_detection_time > DEBOUNCE_SEC:
                    log.info("¡IMPACTO DETECTADO! (Single Tap)")

                    accel_data = accel.acceleration
                    mag_g = (accel_data[0]**2 + accel_data[1]**2 + accel_data[2]**2)**0.5 / 9.81
                    log.info(f"Aceleración: X={accel_data[0]/9.81:.2f}g, Y={accel_data[1]/9.81:.2f}g, Z={accel_data[2]/9.81:.2f}g | Magnitud={mag_g:.2f}g")

                    for _ in range(REPETICIONES):
                        bip_sound.play()
                        time.sleep(PAUSA_ENTRE)

                    last_detection_time = now
                    _ = accel.events['tap']  # Limpiar evento
                    return True
        except Exception as e:
            log.error("Error leyendo evento tap: %s", e)
        time.sleep(0.02)

    log.warning("No se detectó impacto en 7 segundos")
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
            log.warning("No se pudo sincronizar stock: %s", ex)

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
                        registrar_evento_history(db, codigo, 1)
                    except Exception as ex:
                        log.exception("Error Firestore: %s", ex)
                return True, None
            else:
                log.warning(f"No detectado impacto en {espiral_id}")
    return False, "Máquina vacía o no se detectó caída"

# ==============================
# FUNCIONES API
# ==============================
def validar_codigo_api(codigo):
    if not all([URL_API, STORE_ID, API_KEY]):
        return False, "API no configurada"
    url = f"{URL_API}/location/{STORE_ID}/redemption-codes/{codigo.strip()}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("x-api-key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "Código inválido o ya usado"
        return False, f"Error {e.code}"
    except Exception:
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
    except Exception:
        return False, "Error al redimir"

# ==============================
# FUNCIONES DE UI
# ==============================
def pantalla_layout(contenido):
    card = ft.Container(
        content=ft.Column(contenido, alignment="center", horizontal_alignment="center", spacing=20),
        width=500,
        padding=30,
        bgcolor="white",
        border_radius=20,
        shadow=ft.BoxShadow(spread_radius=1, blur_radius=15, color="#AACBFF")
    )
    fondo = ft.Container(
        content=ft.Image(src=FONDO_IMG, fit="cover", expand=True),
        expand=True,
        alignment=ft.alignment.Alignment(0, 0)
    )
    contenido_centrado = ft.Container(content=card, alignment=ft.alignment.Alignment(0, 0), expand=True)
    page.controls.clear()
    page.add(ft.Stack(controls=[fondo, contenido_centrado], expand=True))
    page.update()

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
        actions=[ft.TextButton("OK", on_click=_al_cerrar)]
    )
    page.overlay.append(_alert_firestore)
    _alert_firestore.open = True
    page.update()

def pantalla_principal():
    codigo_input = ft.TextField(label="Ingresá tu código", width=300, text_align=ft.TextAlign.CENTER)
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
            ft.Text("Validando código...", size=24, weight=ft.FontWeight.BOLD, color="#1F3A93")
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
                _mostrar_alert_firestore("Error", msg or "No se pudo entregar", on_ok=pantalla_principal)

        page.run_task(async_proceso)

    pantalla_layout([
        ft.Text("VENDING ARGENTINA", size=32, weight=ft.FontWeight.BOLD, color="#1F3A93"),
        codigo_input,
        ft.Button("VALIDAR", width=200, on_click=validar_codigo),
        mensaje
    ])

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

def probar_conexion_firestore(e):
    if not get_firestore:
        _mostrar_alert_firestore("Firestore", "No configurado")
        return
    try:
        db = get_firestore()
        get_config_stock(db)
        _mostrar_alert_firestore("Firestore", "Conexión OK")
    except Exception as ex:
        _mostrar_alert_firestore("Firestore", f"Error: {str(ex)}")

def ejecutar_prueba_espiral(idx):
    espiral_id = f"espiral{idx + 1}"
    if not all([get_firestore, get_config_stock, update_config_stock, registrar_evento_history]):
        activar_relay(idx)
        _mostrar_alert_firestore("Prueba", "Sin Firestore")
        return
    try:
        db = get_firestore()
        stock = get_config_stock(db)
        cantidad = stock.get(espiral_id, 0)
        if cantidad <= 0:
            _mostrar_alert_firestore("Prueba", "Sin stock")
            return
        activar_relay(idx)
        log.info(f"Prueba espiral {espiral_id}: esperando impacto...")
        if esperar_deteccion():
            nuevo_stock = cantidad - 1
            update_config_stock(db, espiral_id, nuevo_stock)
            registrar_evento_history(db, "PRUEBA", 1)
            STOCK[espiral_id] = nuevo_stock
            _mostrar_alert_firestore("Prueba", "OK - impacto detectado")
        else:
            _mostrar_alert_firestore("Prueba", "No se detectó impacto (ver log)")
    except Exception as ex:
        log.exception("Error en prueba espiral")
        _mostrar_alert_firestore("Error", str(ex))

def pantalla_test_espirales():
    botones = [ft.Button(f"Espiral {i+1}", width=200, on_click=lambda e, idx=i: ejecutar_prueba_espiral(idx)) for i in range(4)]
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
        filas.append(ft.Row([ft.Text(f"Espiral {i+1}", width=120), tf]))

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
        _mostrar_alert_firestore("Stock", "Guardado", on_ok=pantalla_admin)

    pantalla_layout([
        ft.Text("Ajustar Stock", size=26, weight=ft.FontWeight.BOLD, color="#1F3A93"),
        *filas,
        ft.Button("Guardar", on_click=guardar),
        ft.Button("Volver", on_click=lambda e: pantalla_admin())
    ])

# ==============================
# MAIN - Inicialización del sensor AQUÍ
# ==============================
def main(p: ft.Page):
    global page, accel
    page = p
    page.title = "Vending Argentina"
    page.window_width = 800
    page.window_height = 480
    page.bgcolor = "#EAF6FF"
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER

    # Inicializar ADXL345 DENTRO de main()
    accel = None
    try:
        log.info("Inicializando ADXL345 desde main()...")
        log.info("Creando bus I2C...")
        i2c = busio.I2C(board.SCL, board.SDA)
        log.info("Bus I2C creado OK")
        time.sleep(0.5)

        log.info("Creando objeto ADXL345...")
        accel = adafruit_adxl34x.ADXL345(i2c)
        log.info("Objeto ADXL345 creado OK")

        accel.range = adafruit_adxl34x.Range.RANGE_16_G
        accel.data_rate = adafruit_adxl34x.DataRate.RATE_100_HZ
        log.info("Configuración range y data_rate OK")

        log.info("Activando detección de tap (single tap)...")
        accel.enable_tap_detection(
            tap_count=1,
            threshold=20,       # Bajado a 20 para máxima sensibilidad (detecta impactos mínimos)
            duration=20,        # Duración mínima baja para mayor sensibilidad
            latency=50,         # Tiempo muerto bajo para detectar rápido
            window=255
        )
        log.info("Tap detection activada correctamente (configurado para máxima sensibilidad)")

    except Exception as ex:
        log.error("FALLO al inicializar ADXL345 en main():", exc_info=True)
        accel = None

    pantalla_principal()

if __name__ == "__main__":
    try:
        ft.run(main)
    finally:
        GPIO.cleanup()
        pygame.mixer.quit()
        if accel:
            try:
                accel.enable_tap_detection(False)
            except:
                pass