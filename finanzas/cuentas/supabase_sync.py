# ruta: finanzas/cuentas/supabase_sync.py
"""
Sincronización de la base local (SQLite) hacia Supabase.

Idea general:
- SQLite (db.sqlite3) sigue siendo la base "de verdad" de la app: todo
  se lee y se valida contra ella, igual que antes.
- Cada vez que se crea, edita o elimina una Cuenta, o se crea una
  Transaccion (depósito, retiro, transferencia), además de guardar en
  SQLite mandamos una copia a Supabase, a dos tablas nuevas:
  "cuentas" y "transacciones".
- Esa copia en Supabase es la que después consulta el microservicio
  para poder responder preguntas con la IA (Mistral), porque Mistral
  no puede conectarse directo a este SQLite (vive en tu computador /
  tu servidor de Django, no es accesible públicamente).

Diseño importante: si Supabase falla o no hay internet, NINGUNA de
estas funciones debe romper la operación real del usuario (depositar,
retirar, etc.). Por eso todo está en try/except y solo se registra un
aviso en consola con print(); el dinero en SQLite ya quedó guardado
antes de intentar la sincronización.
"""

import threading

import requests
from django.conf import settings

TIMEOUT = 6  # segundos


def _configurado():
    return bool(settings.SUPABASE_URL and settings.SUPABASE_KEY)


def _headers(upsert=False):
    headers = {
        "apikey": settings.SUPABASE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "Content-Type": "application/json",
        # return=minimal: no necesitamos que Supabase nos devuelva la fila,
        # así la respuesta es más rápida y liviana.
        "Prefer": "return=minimal",
    }
    if upsert:
        # merge-duplicates: si ya existe una fila con ese "id", la actualiza
        # en vez de fallar por duplicado (esto es lo que nos permite usar
        # el mismo id de SQLite como id en Supabase).
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    return headers


def _en_segundo_plano(func, *args, **kwargs):
    """Corre func(*args, **kwargs) en un hilo aparte, para que el
    usuario no tenga que esperar a que Supabase responda antes de ver
    la página. Los errores igual quedan controlados dentro de func."""
    hilo = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
    hilo.start()


# ---------------------------------------------------------------------
# CUENTAS
# ---------------------------------------------------------------------

def _upsert_cuenta_sync(cuenta_id, nombre, saldo, fecha_creacion, actualizado_en):
    if not _configurado():
        return
    url = f"{settings.SUPABASE_URL}/rest/v1/cuentas?on_conflict=id"
    fila = {
        "id": cuenta_id,
        "nombre": nombre,
        "saldo": float(saldo),
        "fecha_creacion": fecha_creacion.isoformat(),
        "actualizado_en": actualizado_en.isoformat(),
    }
    try:
        r = requests.post(url, headers=_headers(upsert=True), json=fila, timeout=TIMEOUT)
        if not r.ok:
            print(f"[supabase_sync] No se pudo guardar la cuenta {cuenta_id}: {r.status_code} {r.text}")
    except requests.exceptions.RequestException as e:
        print(f"[supabase_sync] Error de red guardando la cuenta {cuenta_id}: {e}")


def sync_upsert_cuenta(cuenta):
    """Crea o actualiza en Supabase la fila que corresponde a esta Cuenta."""
    _en_segundo_plano(
        _upsert_cuenta_sync,
        cuenta.id, cuenta.nombre, cuenta.saldo,
        cuenta.fecha_creacion, cuenta.actualizado_en,
    )


def _eliminar_cuenta_sync(cuenta_id):
    if not _configurado():
        return
    url = f"{settings.SUPABASE_URL}/rest/v1/cuentas?id=eq.{cuenta_id}"
    try:
        r = requests.delete(url, headers=_headers(), timeout=TIMEOUT)
        if not r.ok:
            print(f"[supabase_sync] No se pudo eliminar la cuenta {cuenta_id}: {r.status_code} {r.text}")
    except requests.exceptions.RequestException as e:
        print(f"[supabase_sync] Error de red eliminando la cuenta {cuenta_id}: {e}")


def sync_eliminar_cuenta(cuenta_id):
    """Elimina en Supabase la cuenta con este id.

    Nota: en la tabla "transacciones" de Supabase, las columnas
    cuenta_origen_id / cuenta_destino_id están creadas con
    "on delete set null" (ver supabase_schema.sql), igual que en
    SQLite, así que el historial de movimientos no se borra."""
    _en_segundo_plano(_eliminar_cuenta_sync, cuenta_id)


# ---------------------------------------------------------------------
# TRANSACCIONES
# ---------------------------------------------------------------------

def _insertar_transaccion_sync(fila):
    if not _configurado():
        return
    url = f"{settings.SUPABASE_URL}/rest/v1/transacciones?on_conflict=id"
    try:
        r = requests.post(url, headers=_headers(upsert=True), json=fila, timeout=TIMEOUT)
        if not r.ok:
            print(f"[supabase_sync] No se pudo guardar la transacción {fila.get('id')}: {r.status_code} {r.text}")
    except requests.exceptions.RequestException as e:
        print(f"[supabase_sync] Error de red guardando la transacción {fila.get('id')}: {e}")


def sync_insertar_transaccion(transaccion):
    """Copia esta Transaccion (depósito, retiro o transferencia) a
    Supabase. Se llama justo después de Transaccion.objects.create(...)."""
    fila = {
        "id": transaccion.id,
        "tipo": transaccion.tipo,
        "cuenta_origen_id": transaccion.cuenta_origen_id,
        "cuenta_destino_id": transaccion.cuenta_destino_id,
        "cuenta_origen_nombre": transaccion.cuenta_origen_nombre,
        "cuenta_destino_nombre": transaccion.cuenta_destino_nombre,
        "monto": float(transaccion.monto),
        "descripcion": transaccion.descripcion,
        "fecha": transaccion.fecha.isoformat(),
    }
    _en_segundo_plano(_insertar_transaccion_sync, fila)