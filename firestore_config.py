"""
Configuración de Firestore usando variables de entorno.
Variables requeridas: FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY.

Colecciones:
- config: documentos espiral1, espiral2, espiral3, espiral4; cada uno con campo "stock" (número).
- history: documentos con (tipo, codigo, cantidad, fecha) para registrar eventos (ej. retiros).
"""
import os
from datetime import datetime, date, time, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # En producción las vars pueden venir del sistema

import firebase_admin
from firebase_admin import credentials, firestore

# Colecciones
COLLECTION_CONFIG = "config"
COLLECTION_HISTORY = "history"

# Config: IDs de los 4 documentos (espirales) y nombre del campo de stock
ESPIRAL_IDS = ["espiral1", "espiral2", "espiral3", "espiral4"]
FIELD_STOCK = "stock"

# Tipo de evento en history (para filtrar por "retiro", etc.)
EVENT_TIPO_RETIRO = "retiro"

_db = None


def build_retiro_event(codigo, cantidad=1, vending_code=None):
    """
    Arma el documento para un evento de tipo "retiro" en la colección history.
    Campos: tipo, codigo, cantidad, fecha y opcionalmente vendingCode (del .env).
    """
    doc = {
        "tipo": EVENT_TIPO_RETIRO,
        "codigo": str(codigo),
        "cantidad": int(cantidad),
        "fecha": firestore.SERVER_TIMESTAMP,
    }
    if vending_code is not None and str(vending_code).strip():
        doc["vendingCode"] = str(vending_code).strip()
    return doc


def _get_credentials():
    project_id = os.getenv("FIREBASE_PROJECT_ID")
    client_email = os.getenv("FIREBASE_CLIENT_EMAIL")
    private_key = os.getenv("FIREBASE_PRIVATE_KEY")

    if not all((project_id, client_email, private_key)):
        raise ValueError(
            "Faltan variables de entorno: FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY. "
            "Usa .env o export en el sistema."
        )

    # La clave suele venir con \n literales; Firestore espera newlines reales
    if isinstance(private_key, str) and "\\n" in private_key:
        private_key = private_key.replace("\\n", "\n")

    # Google Auth exige token_uri y auth_uri en el dict del service account
    return credentials.Certificate({
        "type": "service_account",
        "project_id": project_id,
        "private_key": private_key,
        "client_email": client_email,
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    })


def get_firestore():
    """Devuelve el cliente de Firestore. Inicializa Firebase solo la primera vez."""
    global _db
    if _db is not None:
        return _db

    if not firebase_admin._apps:
        cred = _get_credentials()
        firebase_admin.initialize_app(cred)

    _db = firestore.client()
    return _db


# --- Helpers para colección "config" (stock por espiral) ---


def get_config_stock(db=None):
    """
    Lee el stock de los 4 espirales desde config.
    Devuelve dict { "espiral1": n, "espiral2": n, ... }; si falta un doc, usa 0.
    """
    if db is None:
        db = get_firestore()
    coll = db.collection(COLLECTION_CONFIG)
    out = {}
    for eid in ESPIRAL_IDS:
        doc = coll.document(eid).get()
        if doc.exists:
            data = doc.to_dict() or {}
            out[eid] = data.get(FIELD_STOCK, 0)
        else:
            out[eid] = 0
    return out


def update_config_stock(db, espiral_id, new_stock):
    """Actualiza el campo stock del documento espiral_id en config."""
    if db is None:
        db = get_firestore()
    db.collection(COLLECTION_CONFIG).document(espiral_id).set(
        {FIELD_STOCK: int(new_stock)}, merge=True
    )


# --- Helpers para colección "history" ---


def add_history_event(db, event_dict):
    """
    Añade un documento a la colección history.
    event_dict debe tener al menos: tipo, codigo, cantidad, fecha (puede ser firestore.SERVER_TIMESTAMP).
    """
    if db is None:
        db = get_firestore()
    db.collection(COLLECTION_HISTORY).add(event_dict)


def registrar_evento_history(db, codigo, cantidad=1, vending_code=None):
    """
    Registra un evento de retiro en la colección history (reutilizable).
    codigo: identificador del evento (ej. "PRUEBA", código de usuario).
    cantidad: siempre 1 en pruebas; en dispensado real puede ser 1.
    vending_code: valor de vendingCode del .env; se guarda en el documento para traza.
    """
    if db is None:
        db = get_firestore()
    evento = build_retiro_event(codigo, cantidad, vending_code)
    add_history_event(db, evento)


def get_history_by_date_range(db, date_from: date, date_to: date, limit=500):
    """
    Obtiene los documentos de la colección history entre date_from y date_to (inclusive).
    date_from, date_to: objetos date (solo día; se usa 00:00 y 23:59:59 UTC).
    limit: máximo de documentos a devolver (por defecto 500).
    Devuelve lista de dict con: id, tipo, codigo, cantidad, fecha (str ISO), vendingCode (opcional).
    """
    if db is None:
        db = get_firestore()
    start_dt = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(date_to, time.max, tzinfo=timezone.utc)
    coll = db.collection(COLLECTION_HISTORY)
    query = (
        coll.where("fecha", ">=", start_dt)
        .where("fecha", "<=", end_dt)
        .order_by("fecha", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    out = []
    for doc in query.stream():
        data = doc.to_dict() or {}
        ts = data.get("fecha")
        if ts is None:
            fecha_str = ""
        elif hasattr(ts, "isoformat"):
            fecha_str = ts.isoformat()
        else:
            fecha_str = str(ts)
        out.append({
            "id": doc.id,
            "tipo": data.get("tipo", ""),
            "codigo": data.get("codigo", ""),
            "cantidad": data.get("cantidad", 0),
            "fecha": fecha_str,
            "vendingCode": data.get("vendingCode", ""),
        })
    return out
