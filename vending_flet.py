import flet as ft
import time
import threading
import os
import logging
from datetime import datetime

import RPi.GPIO as GPIO

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
# LOGGING → consola + archivo en /logs/ con timestamp
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
# CONFIGURACIÓN GPIO
# ==============================

RELAY_PINS = [4, 6, 22, 26]  # Cambiar si usás otros pines
TIEMPO_GIRO = 2               # segundos que gira el espiral

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
for pin in RELAY_PINS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)  # LOW = apagado
# STOCK
# ==============================

STOCK = {
    "espiral1": 5,
    "espiral2": 5,
    "espiral3": 5,
    "espiral4": 5,
}
CODIGOS_VALIDOS = ["abc123", "premio2026", "argentina"]
CODIGO_ADMIN = "admin1234"

page = None  # Referencia global a la página de Flet (se asigna en main)
_alert_firestore = None  # Diálogo de prueba Firestore (Page no tiene .dialog en esta versión)

# Ruta del fondo (relativa al directorio de ejecución)
FONDO_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "fondo.jpg")

def main(p: ft.Page):
    global page
    page = p
    page.title = "Vending Argentina"
    page.window_width = 800
    page.window_height = 480
    page.bgcolor = "#EAF6FF"
    page.horizontal_alignment = "center"
    page.vertical_alignment = "center"
    pantalla_principal()

# ==============================
# ACTIVAR RELAY (ACTIVO EN HIGH)
# ==============================
def activar_relay(idx):
    pin = RELAY_PINS[idx]

    GPIO.output(pin, GPIO.HIGH)   # ACTIVAR
    time.sleep(TIEMPO_GIRO)
    GPIO.output(pin, GPIO.LOW)    # DESACTIVAR

# ==============================
# DISPENSAR AUTOMÁTICO
# ==============================
def dispensar_automatico():
    for i, key in enumerate(STOCK):
        if STOCK[key] > 0:
            STOCK[key] -= 1
            activar_relay(i)
            return True
    return False

# ==============================
# TEST ESPIRAL (descuenta en config, registra en history, popup)
# ==============================
def ejecutar_prueba_espiral(idx):
    """
    Ejecuta prueba del espiral idx (0-3): activa relay, descuenta stock en Firestore config,
    registra evento en history con codigo "PRUEBA" y cantidad 1, muestra popup de resultado.
    """
    espiral_id = f"espiral{idx + 1}"
    if get_firestore is None or get_config_stock is None or update_config_stock is None or registrar_evento_history is None:
        activar_relay(idx)
        _mostrar_alert_firestore("Prueba", "Prueba terminada (Firestore no disponible, stock no actualizado).")
        return
    try:
        db = get_firestore()
        stock_actual = get_config_stock(db)
        cantidad = stock_actual.get(espiral_id, 0)
        if cantidad <= 0:
            _mostrar_alert_firestore("Prueba", "No hay stock en este espiral.")
            return
        activar_relay(idx)
        nuevo_stock = cantidad - 1
        update_config_stock(db, espiral_id, nuevo_stock)
        registrar_evento_history(db, "PRUEBA", 1)
        STOCK[espiral_id] = nuevo_stock
        log.info("Prueba espiral %s: stock %s -> %s, evento PRUEBA registrado.", espiral_id, cantidad, nuevo_stock)
        _mostrar_alert_firestore("Prueba", "Prueba terminada correctamente.")
    except Exception as ex:
        log.exception("Error en prueba espiral %s: %s", espiral_id, ex)
        _mostrar_alert_firestore("Error", f"Error al probar espiral:\n{ex}")

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
        bgcolor="white",
        border_radius=20,
        shadow=ft.BoxShadow(
            spread_radius=1,
            blur_radius=15,
            color="#AACBFF"
        )
    )

    # Fondo: imagen a pantalla completa; encima, la tarjeta centrada
    fondo = ft.Container(
        content=ft.Image(
            src=FONDO_IMG,
            fit=ft.ImageFit.COVER,
            opacity=0.85,
        ),
        expand=True,
        alignment=ft.alignment.Alignment(0, 0),
    )
    contenido_stack = ft.Stack(
        [
            fondo,
            ft.Container(
                content=card,
                alignment=ft.alignment.Alignment(0, 0),
                expand=True,
            ),
        ],
        expand=True,
    )

    page.controls.clear()
    page.add(contenido_stack)
    page.update()

# ==============================
# HOME
# ==============================
def pantalla_principal():

    codigo_input = ft.TextField(
        label="Ingresá tu código",
        width=300,
        text_align=ft.TextAlign.CENTER
    )

    mensaje = ft.Text("", color="red")

    def validar_codigo(e):

        codigo = codigo_input.value.lower().strip()

        # ADMIN OCULTO
        if codigo == CODIGO_ADMIN:
            pantalla_admin()
            return

        # CÓDIGOS NORMALES
        if codigo in CODIGOS_VALIDOS:

            pantalla_layout([
                ft.ProgressRing(),
                ft.Text("Entregando premio...",
                        size=24,
                        weight=ft.FontWeight.BOLD,
                        color="#1F3A93")
            ])

            def proceso():

                time.sleep(1)

                if dispensar_automatico():

                    pantalla_layout([
                        ft.Text(" ¡Muchas gracias!",
                                size=28,
                                weight=ft.FontWeight.BOLD,
                                color="#1F3A93")
                    ])
                    time.sleep(4)
                else:
                    pantalla_layout([
                        ft.Text("Sin stock disponible",
                                size=24,
                                color="red")
                    ])
                    time.sleep(3)

                pantalla_principal()

            threading.Thread(target=proceso).start()

        else:
            mensaje.value = "Código inválido"
            page.update()

    pantalla_layout([
        ft.Text(" VENDING ARGENTINA",
                size=32,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),
        codigo_input,
        ft.Button(
            "VALIDAR",
            width=200,
            on_click=validar_codigo
        ),
        mensaje
    ])

# ==============================
# ADMIN
# ==============================
def _cerrar_dialogo_firestore(e):
    """Cierra el diálogo de resultado de prueba Firestore."""
    global _alert_firestore
    if _alert_firestore:
        _alert_firestore.open = False
        page.update()


def _mostrar_alert_firestore(titulo, contenido, on_ok=None):
    """Muestra un AlertDialog en overlay. on_ok: callback opcional al pulsar OK (ej. pantalla_admin)."""
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
        on_dismiss=lambda _: None,
        actions=[ft.TextButton("OK", on_click=_al_cerrar)],
    )
    page.overlay.append(_alert_firestore)
    _alert_firestore.open = True
    page.update()


def probar_conexion_firestore(e):
    """Prueba la conexión a Firestore; escribe en consola, en log y muestra diálogo."""
    log.info("Probando conexión Firestore...")
    print("Probando conexión Firestore...")

    if get_firestore is None or get_config_stock is None:
        err = _firestore_import_error
        if err is None:
            msg = "Firestore no configurado (revisar firestore_config e .env)."
        else:
            msg = f"Error al importar Firestore:\n{err}\n\nSi falta 'firebase_admin', instale en el venv:\npip install firebase-admin"
        log.warning(msg)
        print(msg)
        _mostrar_alert_firestore("Firestore", msg)
        return

    try:
        db = get_firestore()
        get_config_stock(db)
        log.info("Conexión Firestore OK.")
        print("Conexión Firestore OK.")
        _mostrar_alert_firestore("Firestore", "Conexión Firestore OK.")
    except Exception as ex:
        log.exception("Conexión Firestore fallido.")
        print(f"Conexión Firestore fallido: {ex}")
        _mostrar_alert_firestore("Firestore", "Conexión Firestore fallido.")


def pantalla_admin():

    pantalla_layout([
        ft.Text("ADMINISTRACIÓN",
                size=30,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),

        ft.Button(
            " Probar espirales",
            width=250,
            on_click=lambda e: pantalla_test_espirales()
        ),

        ft.Button(
            " Probar conexión Firestore",
            width=250,
            on_click=probar_conexion_firestore
        ),

        ft.Button(
            " Ajustar stock",
            width=250,
            on_click=lambda e: pantalla_stock()
        ),

        ft.Button(
            " Configurar WiFi",
            width=250,
            on_click=lambda e: abrir_wifi()
        ),

        ft.Divider(),

        ft.Button(
            " Salir de admin",
            width=250,
            on_click=lambda e: pantalla_principal()
        )
    ])

# ==============================
# TEST ESPIRALES
# ==============================
def pantalla_test_espirales():

    botones = []

    for i in range(4):
        botones.append(
            ft.Button(
                f"Espiral {i+1}",
                width=200,
                on_click=lambda e, idx=i: ejecutar_prueba_espiral(idx)
            )
        )

    pantalla_layout([
        ft.Text("Probar Espirales",
                size=26,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),
        *botones,
        ft.Divider(),
        ft.Button(
            " Volver",
            width=200,
            on_click=lambda e: pantalla_admin()
        )
    ])

# ==============================
# STOCK
# ==============================
def pantalla_stock():
    global STOCK
    # Cargar stock desde Firestore si hay conexión
    if get_firestore is not None and get_config_stock is not None:
        try:
            db = get_firestore()
            stock_firestore = get_config_stock(db)
            STOCK = dict(stock_firestore)
            log.info("Stock cargado desde Firestore: %s", STOCK)
        except Exception as ex:
            log.warning("No se pudo cargar stock desde Firestore, usando valores locales: %s", ex)

    inputs = {}
    filas = []

    for i in range(4):
        key = f"espiral{i+1}"

        tf = ft.TextField(
            value=str(STOCK[key]),
            width=100,
            text_align=ft.TextAlign.CENTER
        )

        inputs[key] = tf

        filas.append(
            ft.Row(
                [
                    ft.Text(f"Espiral {i+1}", width=120),
                    tf
                ],
                alignment=ft.MainAxisAlignment.CENTER
            )
        )

    def guardar(e):
        global STOCK
        for key in inputs:
            try:
                STOCK[key] = int(inputs[key].value)
            except (ValueError, TypeError):
                pass
        # Persistir en Firestore si hay conexión
        if get_firestore is not None and update_config_stock is not None:
            try:
                db = get_firestore()
                for key in STOCK:
                    update_config_stock(db, key, STOCK[key])
                log.info("Stock guardado en Firestore: %s", STOCK)
                _mostrar_alert_firestore("Stock", "Stock actualizado.", on_ok=pantalla_admin)
            except Exception as ex:
                log.exception("Error al guardar stock en Firestore: %s", ex)
                _mostrar_alert_firestore("Error", f"No se pudo guardar en Firestore:\n{ex}", on_ok=pantalla_admin)
        else:
            _mostrar_alert_firestore("Stock", "Stock actualizado (solo local).", on_ok=pantalla_admin)

    pantalla_layout([
        ft.Text("Ajustar Stock",
                size=26,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),
        *filas,
        ft.Button(" Guardar cambios", on_click=guardar),
        ft.Button(" Volver", on_click=lambda e: pantalla_admin())
    ])

# ==============================
# WIFI
# ==============================
def abrir_wifi():
    os.system("nm-connection-editor &")

# ==============================
try:
    ft.run(main)
finally:
    GPIO.cleanup()