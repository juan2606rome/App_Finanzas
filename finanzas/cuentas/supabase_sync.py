# ruta: finanzas/cuentas/supabase_sync.py
"""
Sincronización de la base local (SQLite) hacia Supabase, A TRAVÉS DE LOS
MICROSERVICIOS.

Idea general:
- SQLite sigue siendo la base "de verdad" de la app.
- Cada vez que se crea, edita o elimina una Cuenta, o se crea una
  Transaccion, Django manda la operación a un microservicio, y es el
  microservicio quien escribe en Supabase. Django ya NO tiene la clave de
  Supabase: solo la tienen los microservicios.
- Las tres operaciones que pide el proyecto:
      INSERTAR   -> POST   /api/cuentas        y  POST /api/transacciones
      ACTUALIZAR -> PUT    /api/cuentas/<id>
      ELIMINAR   -> DELETE /api/cuentas/<id>
- Resiliencia: si el microservicio de Python falla, se intenta con el de
  Java, luego con el de Node.js y por último con el de Go
  (ver mysite/microservicios.py).

Las operaciones se ponen en una COLA y las procesa un solo hilo en segundo
plano, una por una y en orden. Así el usuario no espera, y además una
cuenta siempre llega a Supabase antes que sus transacciones (la tabla
transacciones apunta a cuentas, y si llegaran al revés fallaría).

Si TODOS los microservicios fallan, no se rompe nada: el dinero ya quedó
guardado en SQLite y solo se imprime un aviso en la consola.
"""

import queue
import threading

from django.conf import settings

from mysite.microservicios import TodosFallaron, llamar

_cola = queue.Queue()
_hilo = None
_candado = threading.Lock()


def _enviar(metodo, ruta, cuerpo, descripcion):
    try:
        respuesta, servicio, fallos = llamar(
            metodo, ruta, json=cuerpo, timeout=settings.MS_TIMEOUT_ESCRITURA
        )
    except TodosFallaron as e:
        print(f"[sync] {descripcion}: NO se pudo sincronizar, fallaron todos los microservicios -> {e}")
        return

    aviso = f" (antes fallaron: {', '.join(fallos)})" if fallos else ""
    if respuesta.ok:
        print(f"[sync] {descripcion}: OK vía microservicio {servicio}{aviso}")
    else:
        print(f"[sync] {descripcion}: {servicio} respondió {respuesta.status_code}: {respuesta.text[:200]}")


def _trabajador():
    while True:
        metodo, ruta, cuerpo, descripcion = _cola.get()
        try:
            _enviar(metodo, ruta, cuerpo, descripcion)
        except Exception as e:  # el hilo nunca debe morirse por un error suelto
            print(f"[sync] {descripcion}: error inesperado: {e}")
        finally:
            _cola.task_done()


def _encolar(metodo, ruta, cuerpo, descripcion):
    global _hilo
    with _candado:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_trabajador, daemon=True)
            _hilo.start()
    _cola.put((metodo, ruta, cuerpo, descripcion))


# ---------------------------------------------------------------------
# CUENTAS
# ---------------------------------------------------------------------

def _cuerpo_cuenta(cuenta):
    # Se arma aquí (no dentro del hilo) para congelar los valores de este
    # momento, aunque la cuenta cambie de nuevo antes de que se envíe.
    return {
        "id": cuenta.id,
        "nombre": cuenta.nombre,
        "saldo": float(cuenta.saldo),
        "fecha_creacion": cuenta.fecha_creacion.isoformat(),
        "actualizado_en": cuenta.actualizado_en.isoformat(),
    }


def sync_crear_cuenta(cuenta):
    """INSERTAR: una cuenta nueva."""
    _encolar("POST", "/api/cuentas", _cuerpo_cuenta(cuenta), f"insertar cuenta {cuenta.id}")


def sync_actualizar_cuenta(cuenta):
    """ACTUALIZAR: cambió el nombre o el saldo de una cuenta."""
    _encolar("PUT", f"/api/cuentas/{cuenta.id}", _cuerpo_cuenta(cuenta), f"actualizar cuenta {cuenta.id}")


def sync_eliminar_cuenta(cuenta_id):
    """ELIMINAR: se borró una cuenta.

    En la tabla "transacciones" de Supabase, cuenta_origen_id y
    cuenta_destino_id están con "on delete set null" (ver
    supabase_schema.sql), igual que en SQLite, así que el historial de
    movimientos no se borra."""
    _encolar("DELETE", f"/api/cuentas/{cuenta_id}", None, f"eliminar cuenta {cuenta_id}")


# ---------------------------------------------------------------------
# TRANSACCIONES
# ---------------------------------------------------------------------

def sync_insertar_transaccion(transaccion):
    """INSERTAR: copia esta Transaccion (depósito, retiro o transferencia)
    al historial. Se llama justo después de Transaccion.objects.create(...)."""
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
    _encolar("POST", "/api/transacciones", fila, f"insertar transacción {transaccion.id}")