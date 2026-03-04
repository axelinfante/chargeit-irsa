import flet as ft
import time
import threading
import os
import RPi.GPIO as GPIO# ==============================
# CONFIGURACIÓN GPIO
# ==============================

RELAY_PINS = [4, 6, 22, 26]  # Cambiar si usás otros pines
TIEMPO_GIRO = 2               # segundos que gira el espiralGPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)# RELAY ACTIVO EN HIGH
for pin in RELAY_PINS:
    GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)  # LOW = apagado# ==============================
# STOCK
# ==============================

STOCK = {
    "espiral1": 5,
    "espiral2": 5,
    "espiral3": 5,
    "espiral4": 5,
}CODIGOS_VALIDOS = ["abc123", "premio2026", "argentina"]
CODIGO_ADMIN = "admin1234"# ==============================
def main(page: ft.Page):page.title = "Vending Argentina"
page.window_width = 800
page.window_height = 480
page.bgcolor = "#EAF6FF"
page.horizontal_alignment = "center"
page.vertical_alignment = "center"

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
# TEST ESPIRAL (NO RESTA STOCK)
# ==============================
def dispensar_test(idx):
    activar_relay(idx)

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

    page.controls.clear()
    page.add(
        ft.Container(
            content=card,
            alignment=ft.alignment.Alignment(0, 0),
            expand=True
        )
    )
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
        ft.ElevatedButton(
            "VALIDAR",
            width=200,
            on_click=validar_codigo
        ),
        mensaje
    ])

# ==============================
# ADMIN
# ==============================
def pantalla_admin():

    pantalla_layout([
        ft.Text("ADMINISTRACIÓN",
                size=30,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),

        ft.ElevatedButton(
            " Probar espirales",
            width=250,
            on_click=lambda e: pantalla_test_espirales()
        ),

        ft.ElevatedButton(
            " Ajustar stock",
            width=250,
            on_click=lambda e: pantalla_stock()
        ),

        ft.ElevatedButton(
            " Configurar WiFi",
            width=250,
            on_click=lambda e: abrir_wifi()
        ),

        ft.Divider(),

        ft.ElevatedButton(
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
            ft.ElevatedButton(
                f"Espiral {i+1}",
                width=200,
                on_click=lambda e, idx=i: dispensar_test(idx)
            )
        )

    pantalla_layout([
        ft.Text("Probar Espirales",
                size=26,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),
        *botones,
        ft.Divider(),
        ft.ElevatedButton(
            " Volver",
            width=200,
            on_click=lambda e: pantalla_admin()
        )
    ])

# ==============================
# STOCK
# ==============================
def pantalla_stock():

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
        for key in inputs:
            try:
                STOCK[key] = int(inputs[key].value)
            except:
                pass
        pantalla_admin()

    pantalla_layout([
        ft.Text("Ajustar Stock",
                size=26,
                weight=ft.FontWeight.BOLD,
                color="#1F3A93"),
        *filas,
        ft.ElevatedButton(" Guardar cambios", on_click=guardar),
        ft.ElevatedButton(" Volver", on_click=lambda e: pantalla_admin())
    ])

# ==============================
# WIFI
# ==============================
def abrir_wifi():
    os.system("nm-connection-editor &")

pantalla_principal()# ==============================
try:
    ft.app(target=main)
finally:
    GPIO.cleanup()