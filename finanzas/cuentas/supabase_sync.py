# ruta: finanzas/cuentas/supabase_sync.py
"""
Sincronización de la base de Django hacia Supabase, A TRAVÉS DE LOS
MICROSERVICIOS.

Idea general:
- La base de Django (SQLite en tu PC, PostgreSQL en Render) es la "de verdad".
- Cada vez que se crea, edita o elimina una Cuenta, o se crea una Transaccion,
  la operación se GUARDA primero en la tabla SyncPendiente (una bandeja de
  salida dentro de la misma base de Django). Un hilo en segundo plano la
  manda a un microservicio y, si sale bien, la borra de la bandeja.
- Si TODOS los microservicios están apagados, la operación se queda en la
  bandeja (no se pierde) y se reintenta:
      * el hilo insiste ~3 minutos, mientras despierta los microservicios;
      * y se reanuda solo al entrar a Inicio, a Chat con IA o a la página
        Microservicios (ver reanudar_si_hay_pendientes);
      * o a mano con el botón "Sincronizar con Supabase".
- AUTOMÁTICO: en cuanto Django nota que un microservicio pasó de apagado a
  encendido (ver registrar_al_encender), lo PRIMERO que hace es vaciar la
  bandeja y reconciliar todo con Supabase (igual que el botón "Sincronizar
  todo"), sin que tengas que pulsar nada.
- Las escrituras en los microservicios son "upsert" (insertar o reemplazar
  por id), así que reenviar la misma operación dos veces es seguro.

Las tres operaciones que pide el proyecto:
      INSERTAR   -> POST   /api/cuentas        y  POST /api/transacciones
      ACTUALIZAR -> PUT    /api/cuentas/<id>
      ELIMINAR   -> DELETE /api/cuentas/<id>
"""

import threading
import time

from django.conf import settings
from django.db import connection

from mysite.microservicios import (
    TodosFallaron, despertar_en_segundo_plano, estado_todos, llamar, registrar_al_encender,
)

from .models import Cuenta, SyncPendiente, Transaccion

# Solo un hilo/petición procesa la bandeja a la vez (así no se envía dos veces
# lo mismo ni se desordenan las operaciones).
_candado_proceso = threading.Lock()
_candado_hilo = threading.Lock()
_hilo = None

# Cada cuánto y cuántas veces reintenta el hilo cuando todos están apagados.
ESPERA_ENTRE_REINTENTOS = 15  # segundos
MAX_REINTENTOS_HILO = 12      # 12 x 15 s = 3 minutos
# Mínimo de segundos entre dos reconciliaciones completas automáticas
# (evita re-subir todo cada vez que un servicio parpadea entre apagado/encendido).
COOLDOWN_RECONCILIACION = 300


# ---------------------------------------------------------------------
# Envío de UNA operación
# ---------------------------------------------------------------------

def _enviar_una(pendiente):
    """
    Intenta mandar una operación. Devuelve:
      "ok"       -> se envió bien (o fue rechazada por datos malos: se borra).
      "reintentar" -> ningún microservicio pudo atenderla ahora (siguen apagados).
    """
    try:
        respuesta, servicio, fallos = llamar(
            pendiente.metodo, pendiente.ruta,
            json=pendiente.cuerpo, timeout=settings.MS_TIMEOUT_ESCRITURA,
            plazo_total=settings.MS_TIMEOUT_ESCRITURA * 2,
        )
    except TodosFallaron as e:
        pendiente.intentos += 1
        pendiente.ultimo_error = str(e)[:300]
        pendiente.save(update_fields=["intentos", "ultimo_error"])
        print(f"[sync] {pendiente.descripcion}: sin microservicios disponibles -> {e}")
        return "reintentar"

    aviso = f" (antes fallaron: {', '.join(fallos)})" if fallos else ""
    if respuesta.ok:
        print(f"[sync] {pendiente.descripcion}: OK vía microservicio {servicio}{aviso}")
    else:
        # Error 4xx "de verdad" (datos incompletos, etc.): reenviar no lo arregla.
        print(f"[sync] {pendiente.descripcion}: {servicio} respondió {respuesta.status_code}, se descarta: {respuesta.text[:200]}")
    return "ok"


def procesar_pendientes(max_segundos=None, espera_candado=None):
    """
    Envía la bandeja en orden. Se detiene en la primera operación que no se
    pueda enviar (para no desordenar). Devuelve True si la bandeja quedó vacía.

    Si otro hilo ya la está procesando, espera `espera_candado` segundos (o lo
    que haga falta si es None). Si no logra el turno a tiempo devuelve False:
    así una petición web nunca se queda colgada esperando al vigilante.

    Las operaciones NO se descartan por fallar muchas veces: si los
    microservicios están apagados, se quedan guardadas hasta que alguno
    encienda. (Solo se descartan las rechazadas por datos malos, error 4xx,
    porque reenviarlas no las arregla.)
    """
    inicio = time.time()
    if not _candado_proceso.acquire(timeout=-1 if espera_candado is None else espera_candado):
        return False
    try:
        while True:
            pendiente = SyncPendiente.objects.order_by("id").first()
            if pendiente is None:
                return True
            if max_segundos is not None and time.time() - inicio > max_segundos:
                return False
            if _enviar_una(pendiente) == "ok":
                SyncPendiente.objects.filter(pk=pendiente.pk).delete()
            else:
                return False
    finally:
        _candado_proceso.release()


# ---------------------------------------------------------------------
# Hilo en segundo plano
# ---------------------------------------------------------------------

def _trabajador():
    try:
        reintentos = 0
        while True:
            if procesar_pendientes():
                break  # bandeja vacía
            reintentos += 1
            if reintentos == 1:
                # Hay microservicios dormidos: los despertamos mientras esperamos.
                despertar_en_segundo_plano()
            if reintentos > MAX_REINTENTOS_HILO:
                print("[sync] Los microservicios siguen apagados; las operaciones quedan guardadas y se reintentan luego.")
                break
            time.sleep(ESPERA_ENTRE_REINTENTOS)
    except Exception as e:  # el hilo nunca debe morirse en silencio
        print(f"[sync] error inesperado en el hilo: {e}")
    finally:
        connection.close()  # cada hilo cierra su propia conexión a la base


def iniciar_hilo():
    """Arranca el hilo de sincronización si no está corriendo ya."""
    global _hilo
    with _candado_hilo:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_trabajador, daemon=True)
            _hilo.start()


# ---------------------------------------------------------------------
# Vigilante permanente (sincronización 100% automática)
# ---------------------------------------------------------------------
# Un hilo que vive mientras Django esté encendido. Cada INTERVALO_VIGILANTE
# segundos mira la bandeja; si hay operaciones pendientes, intenta enviarlas
# (esa petición también despierta a los microservicios dormidos). Así, apenas
# UN microservicio termina de encender, lo pendiente se sube solo, sin que
# tengas que abrir ninguna página ni pulsar nada.
INTERVALO_VIGILANTE = 15  # segundos
_vigilante = None


def _bucle_vigilante():
    time.sleep(10)  # dar tiempo a que Django termine de arrancar
    ultimo_despertar = 0.0
    # Lo PRIMERO que revisa: ¿hay algún microservicio ya encendido? Si lo hay,
    # el aviso "acaba de encender" dispara la reconciliación automática.
    try:
        estado_todos(timeout=10)
    except Exception as e:
        print(f"[sync] vigilante: revisión inicial falló: {e}")
    finally:
        connection.close()
    while True:
        try:
            if SyncPendiente.objects.exists():
                if not procesar_pendientes(max_segundos=60):
                    # Siguen apagados: los despertamos (máx. una vez por minuto).
                    if time.time() - ultimo_despertar > 60:
                        despertar_en_segundo_plano()
                        ultimo_despertar = time.time()
        except Exception as e:  # p. ej. la tabla aún no existe antes de migrar
            print(f"[sync] vigilante: {e}")
        finally:
            connection.close()
        time.sleep(INTERVALO_VIGILANTE)


def iniciar_vigilante():
    """Arranca (una sola vez por proceso) el hilo vigilante."""
    global _vigilante
    with _candado_hilo:
        if _vigilante is None or not _vigilante.is_alive():
            _vigilante = threading.Thread(target=_bucle_vigilante, daemon=True, name="sync-vigilante")
            _vigilante.start()


def cantidad_pendientes():
    return SyncPendiente.objects.count()


def reanudar_si_hay_pendientes():
    """Si quedaron operaciones sin enviar, reanuda el envío en segundo plano.
    Es barato (un COUNT): se llama desde Inicio, Chat con IA y Microservicios."""
    try:
        if cantidad_pendientes() > 0:
            iniciar_hilo()
    except Exception as e:  # p. ej. la tabla aún no existe antes de migrar
        print(f"[sync] no se pudo revisar la bandeja: {e}")


def _encolar(metodo, ruta, cuerpo, descripcion):
    SyncPendiente.objects.create(
        metodo=metodo, ruta=ruta, cuerpo=cuerpo, descripcion=descripcion
    )
    iniciar_hilo()


# ---------------------------------------------------------------------
# CUENTAS
# ---------------------------------------------------------------------

def _cuerpo_cuenta(cuenta):
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
    """ELIMINAR: se borró una cuenta. En Supabase los movimientos quedan
    (on delete set null), igual que en Django."""
    _encolar("DELETE", f"/api/cuentas/{cuenta_id}", None, f"eliminar cuenta {cuenta_id}")


# ---------------------------------------------------------------------
# TRANSACCIONES
# ---------------------------------------------------------------------

def _cuerpo_transaccion(t):
    return {
        "id": t.id,
        "tipo": t.tipo,
        "cuenta_origen_id": t.cuenta_origen_id,
        "cuenta_destino_id": t.cuenta_destino_id,
        "cuenta_origen_nombre": t.cuenta_origen_nombre,
        "cuenta_destino_nombre": t.cuenta_destino_nombre,
        "monto": float(t.monto),
        "descripcion": t.descripcion,
        "fecha": t.fecha.isoformat(),
    }


def sync_insertar_transaccion(transaccion):
    """INSERTAR: copia esta Transaccion al historial de Supabase."""
    _encolar("POST", "/api/transacciones", _cuerpo_transaccion(transaccion),
             f"insertar transacción {transaccion.id}")


# ---------------------------------------------------------------------
# SINCRONIZACIÓN AUTOMÁTICA al encender un microservicio
# ---------------------------------------------------------------------
_candado_reconciliar = threading.Lock()
_ultima_reconciliacion = 0.0


def _reconciliar(nombre):
    """Se ejecuta en un hilo cuando el microservicio `nombre` acaba de encender:
    1) sube TODO lo que hay en Django (reconciliación completa, si hace falta), y
    2) vacía la bandeja."""
    global _ultima_reconciliacion
    try:
        if _candado_reconciliar.acquire(blocking=False):
            try:
                ya_hay_resincronizacion = SyncPendiente.objects.filter(
                    descripcion__startswith="resincronizar"
                ).exists()
                reciente = time.time() - _ultima_reconciliacion < COOLDOWN_RECONCILIACION
                if not ya_hay_resincronizacion and not reciente:
                    cuentas, transacciones = sync_todo()
                    _ultima_reconciliacion = time.time()
                    print(f"[sync] {nombre} encendió: reconciliación automática "
                          f"({cuentas} cuenta(s), {transacciones} movimiento(s)).")
            finally:
                _candado_reconciliar.release()
        procesar_pendientes()
    except Exception as e:  # p. ej. la tabla aún no existe antes de migrar
        print(f"[sync] reconciliación automática falló: {e}")
    finally:
        connection.close()


# Cada vez que cualquier parte de Django (una petición, el vigilante o la
# pantalla Microservicios) nota que un servicio encendió, se llama a _reconciliar.
registrar_al_encender(_reconciliar)


# ---------------------------------------------------------------------
# SINCRONIZAR TODO (reparar lo que quedó sin subir)
# ---------------------------------------------------------------------

def sync_todo():
    """
    Pone en la bandeja TODAS las cuentas y TODAS las transacciones que hay
    ahora en la base de Django (primero las cuentas, luego los movimientos,
    porque los movimientos apuntan a cuentas). Como el envío es "upsert", lo
    que ya estaba en Supabase se sobrescribe igual y lo que faltaba se crea.
    Devuelve (cuentas, transacciones) encoladas.
    """
    cuentas = list(Cuenta.objects.all())
    transacciones = list(Transaccion.objects.order_by("id"))
    filas = [
        SyncPendiente(metodo="POST", ruta="/api/cuentas", cuerpo=_cuerpo_cuenta(c),
                      descripcion=f"resincronizar cuenta {c.id}")
        for c in cuentas
    ] + [
        SyncPendiente(metodo="POST", ruta="/api/transacciones", cuerpo=_cuerpo_transaccion(t),
                      descripcion=f"resincronizar transacción {t.id}")
        for t in transacciones
    ]
    SyncPendiente.objects.bulk_create(filas)
    iniciar_hilo()
    return len(cuentas), len(transacciones)