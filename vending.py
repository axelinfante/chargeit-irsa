from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.gridlayout import GridLayout
from kivy.core.window import Window
from kivy.clock import Clock           # ← Agregá esta línea
import RPi.GPIO as GPIO
import time
import subprocess

# GPIO - 4 motores
MOTOR_PINS = [17, 18, 27, 22]
GPIO.setmode(GPIO.BCM)
for pin in MOTOR_PINS:
    GPIO.setup(pin, GPIO.OUT)

# Datos de prueba (locales)
CODIGOS_VALIDOS = {"ABC123", "PLAY2025", "ARGENTINA10", "TEST001", "MESSI23"}
CODIGOS_USADOS = set()

STOCK = {"espiral1": 12, "espiral2": 8, "espiral3": 15, "espiral4": 5}

ADMIN_CODE = "admin123456"      # Código para entrar al menú admin
ADMIN_PASS = "admin123"         # Contraseña secundaria dentro del menú

TIEMPO_GIRO = 2.5

def dispensar(espiral_index):
    pin = MOTOR_PINS[espiral_index]
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(TIEMPO_GIRO)
    GPIO.output(pin, GPIO.LOW)

class VendingApp(App):
    def build(self):
        Window.fullscreen = 'auto'
        layout = BoxLayout(orientation='vertical', padding=30, spacing=20)
        
        layout.add_widget(Label(text="Ingresa tu código alfanumérico", font_size=32, size_hint=(1, 0.15)))
        
        self.codigo_input = TextInput(multiline=False, font_size=48, size_hint=(1, 0.25), hint_text="Código aquí...")
        layout.add_widget(self.codigo_input)
        
        btn = Button(text="Enviar", font_size=36, size_hint=(1, 0.2), background_color=(0.2, 0.6, 1, 1))
        btn.bind(on_press=self.procesar_codigo)
        layout.add_widget(btn)
        
        return layout

    def procesar_codigo(self, instance):
        codigo = self.codigo_input.text.strip().upper()
        print(f"[DEBUG] Código ingresado: '{codigo}'")  # ← Para ver en consola
        
        if not codigo:
            self.mostrar_popup("Error", "Ingresa un código válido.", duracion=5)
            return
        
        # Menú admin
        if codigo == ADMIN_CODE.upper():
            print("[DEBUG] Código ADMIN detectado → abriendo menú")
            self.codigo_input.text = ""
            self.abrir_menu_oculto()
            return
        
        # Validación normal
        if codigo in CODIGOS_VALIDOS and codigo not in CODIGOS_USADOS:
            print(f"[DEBUG] Código válido: {codigo}")
            self.mostrar_seleccion_espiral(codigo)
        else:
            print(f"[DEBUG] Código inválido o usado: {codigo}")
            self.mostrar_popup("Error", "Código inválido, ya usado o expirado.", duracion=6)

        self.codigo_input.text = ""

    def mostrar_seleccion_espiral(self, codigo):
        content = GridLayout(cols=1, spacing=15, padding=20)
        content.add_widget(Label(text="Elige un juguete disponible:", font_size=28))
        
        for i in range(4):
            key = f'espiral{i+1}'
            stock = STOCK.get(key, 0)
            if stock > 0:
                btn = Button(text=f"{key.capitalize()} (Stock: {stock})", font_size=26, size_hint=(1, None), height=80)
                btn.bind(on_press=lambda x, idx=i, c=codigo: self.confirmar_dispensar(idx, c))
                content.add_widget(btn)
        
        if all(STOCK.get(f'espiral{i+1}', 0) <= 0 for i in range(4)):
            content.add_widget(Label(text="No hay stock disponible", font_size=24, color=(1,0,0,1)))
        
        popup = Popup(title="Selecciona producto", content=content, size_hint=(0.85, 0.75))
        popup.open()

    def confirmar_dispensar(self, espiral_index, codigo):
        self.popup_espiral.dismiss() if hasattr(self, 'popup_espiral') else None
        key = f'espiral{espiral_index+1}'
        stock = STOCK.get(key, 0)
        
        if stock <= 0:
            self.mostrar_popup("Error", "Stock agotado.", duracion=5)
            return
        
        CODIGOS_USADOS.add(codigo)
        dispensar(espiral_index)
        STOCK[key] = stock - 1
        
        self.mostrar_popup("¡Éxito!", f"Producto dispensado!\nStock restante en {key}: {STOCK[key]}", duracion=6)

    def abrir_menu_oculto(self):
        content = GridLayout(cols=1, spacing=15, padding=25)
        content.add_widget(Label(text="Menú Administrador", font_size=32))
        
        pass_input = TextInput(hint_text="Contraseña admin", password=True, font_size=28)
        content.add_widget(pass_input)
        
        btn = Button(text="Entrar", font_size=28)
        btn.bind(on_press=lambda x: self.verificar_pass_admin(pass_input.text))
        content.add_widget(btn)
        
        self.popup_admin = Popup(title="Acceso Admin", content=content, size_hint=(0.8, 0.6))
        self.popup_admin.open()

    def verificar_pass_admin(self, password):
        if password.strip() == ADMIN_PASS:
            print("[DEBUG] Contraseña admin correcta → abriendo panel completo")
            self.popup_admin.dismiss()
            self.mostrar_menu_admin_full()
        else:
            self.mostrar_popup("Acceso denegado", "Contraseña incorrecta.", duracion=5)

    def mostrar_menu_admin_full(self):
        content = GridLayout(cols=1, spacing=15, padding=20)
        content.add_widget(Label(text="Opciones Administrador", font_size=30))
        
        btn_wifi = Button(text="Configurar WiFi", font_size=26)
        btn_wifi.bind(on_press=lambda x: self.mostrar_config_wifi())
        content.add_widget(btn_wifi)
        
        for i in range(4):
            btn = Button(text=f"Probar espiral {i+1}", font_size=24)
            btn.bind(on_press=lambda x, idx=i: dispensar(idx))
            content.add_widget(btn)
        
        btn_stock = Button(text="Gestionar stock", font_size=26)
        btn_stock.bind(on_press=lambda x: self.mostrar_gestion_stock())
        content.add_widget(btn_stock)
        
        Popup(title="Panel Admin", content=content, size_hint=(0.85, 0.85)).open()

    def mostrar_config_wifi(self):
        content = GridLayout(cols=1, spacing=15, padding=20)
        ssid = TextInput(hint_text="Nombre de red (SSID)", font_size=24)
        content.add_widget(ssid)
        pwd = TextInput(hint_text="Contraseña WiFi", password=True, font_size=24)
        content.add_widget(pwd)
        btn = Button(text="Conectar", font_size=26)
        btn.bind(on_press=lambda x: self.conectar_wifi(ssid.text, pwd.text))
        content.add_widget(btn)
        Popup(title="Configuración WiFi", content=content, size_hint=(0.8, 0.6)).open()

    def conectar_wifi(self, ssid, password):
        if not ssid or not password:
            self.mostrar_popup("Error", "Completa ambos campos.", duracion=5)
            return
        try:
            subprocess.run(['nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password], check=True)
            self.mostrar_popup("Éxito", "Conectado correctamente.", duracion=5)
        except Exception as e:
            self.mostrar_popup("Fallo", f"No se pudo conectar:\n{str(e)}", duracion=8)

    def mostrar_gestion_stock(self):
        content = GridLayout(cols=1, spacing=15, padding=20)
        content.add_widget(Label(text="Actualizar stock", font_size=28))
        
        for i in range(4):
            key = f'espiral{i+1}'
            stock_actual = STOCK.get(key, 0)
            inp = TextInput(hint_text=f"{key} - Actual: {stock_actual}", multiline=False, font_size=24)
            content.add_widget(inp)
            inp.bind(on_text_validate=lambda x, k=key, i=inp: self.guardar_stock(k, i.text))
        
        Popup(title="Gestión de Stock", content=content, size_hint=(0.8, 0.7)).open()

    def guardar_stock(self, key, valor):
        try:
            nuevo = int(valor)
            STOCK[key] = nuevo
            self.mostrar_popup("Guardado", f"{key} actualizado a {nuevo}", duracion=5)
        except ValueError:
            self.mostrar_popup("Error", "Ingresa solo números.", duracion=5)

    def mostrar_popup(self, titulo, texto, duracion=6):
        """Popup que se cierra solo después de 'duracion' segundos"""
        content = BoxLayout(orientation='vertical', padding=20, spacing=10)
        content.add_widget(Label(text=texto, font_size=24, halign='center'))
        btn_ok = Button(text="OK", size_hint=(1, 0.3), font_size=26)
        content.add_widget(btn_ok)
        
        popup = Popup(title=titulo, content=content, size_hint=(0.8, 0.5))
        btn_ok.bind(on_press=popup.dismiss)
        popup.open()
        
        # Cierra automáticamente después de duracion segundos
        def auto_close(dt):
            if popup._is_open:
                popup.dismiss()
        Clock.schedule_once(auto_close, duracion)

if __name__ == '__main__':
    try:
        VendingApp().run()
    finally:
        GPIO.cleanup()