# ruta: finanzas/mysite/microservicios.py
"""
Resiliencia (failover) entre microservicios.

Los cuatro microservicios (Python, Java, Node.js y Go) exponen exactamente
las mismas rutas y hacen lo mismo. Django llama siempre por esta función:

    respuesta, servicio, fallos = llamar("GET", "/api/tasa")

Se intenta primero el de Python; si falla se intenta con Java, luego con
Node.js y por último con Go. El orden y las URLs se configuran en
settings.MICROSERVICIOS.

Qué cuenta como "fallo" (y por eso se pasa al siguiente):
  - No se pudo conectar / se acabó el tiempo de espera.
  - El servicio respondió con un error del servidor (500 o más).
  - El servicio respondió 429 (demasiadas peticiones) o 408 (tiempo agotado):
    significa "ahora no puedo atenderte", así que se prueba con otro.
Cualquier otro error 4xx (por ejemplo 400 por datos incompletos) NO es fallo
del servicio: los demás responderían igual, así que se devuelve tal cual.
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

# Códigos 4xx que significan "este servicio no puede atenderte ahora".
CODIGOS_DE_FALLO = {408, 429}

# Tiempo máximo (segundos) que una sola llamada a llamar() puede pasar probando
# servicios en total. Sin esto, con 4 servicios dormidos y 25 s de espera cada
# uno, una sola petición podía tardar más de 100 s (y gunicorn la mataba a los
# 30 s). Si en Render subes el timeout de gunicorn (--timeout 120), puedes
# subir también esta variable de entorno.
PLAZO_TOTAL_DEFECTO = int(os.environ.get("MS_PLAZO_TOTAL", "27"))

# Segundos que se "descarta" un servicio después de fallar, para no perder
# 15-45 s intentándolo de nuevo en cada petición mientras sigue dormido.
SEGUNDOS_EN_PAUSA = 30

# Segundos máximos para CONECTAR (aparte del tiempo de espera de la respuesta).
TIMEOUT_CONEXION = 5


class TodosFallaron(Exception):
    """Ninguno de los microservicios pudo atender la petición."""

    def __init__(self, fallos):
        self.fallos = fallos
        detalle = ", ".join(fallos) if fallos else "no hay ninguna URL de microservicio configurada"
        super().__init__(detalle)


# ---------------------------------------------------------------------
# Memoria del estado de cada servicio
# ---------------------------------------------------------------------
# Guardamos, por servicio, si lo vimos encendido y hasta cuándo lo damos por
# caído. Sirve para dos cosas:
#   1) No perder tiempo: un servicio que acaba de fallar se salta unos segundos.
#   2) Detectar el momento EXACTO en que un servicio pasa de apagado a
#      encendido, para avisar a quien quiera enterarse (por ejemplo, la
#      sincronización con Supabase, que se dispara sola en ese momento).
_candado_estado = threading.Lock()
_estado = {}          # nombre -> {"activo": bool, "en_pausa_hasta": float}
_al_encender = []     # funciones f(nombre) que se llaman cuando un servicio enciende


def registrar_al_encender(funcion):
    """Registra una función que se llamará (en un hilo aparte) con el nombre del
    servicio cada vez que uno pase de apagado/desconocido a encendido."""
    if funcion not in _al_encender:
        _al_encender.append(funcion)


def _marcar(nombre, activo):
    """Anota el estado de un servicio. Si acaba de encender, avisa a los suscritos."""
    with _candado_estado:
        previo = _estado.get(nombre)
        _estado[nombre] = {
            "activo": activo,
            "en_pausa_hasta": 0.0 if activo else time.time() + SEGUNDOS_EN_PAUSA,
        }
        acaba_de_encender = activo and (previo is None or not previo["activo"])
    if acaba_de_encender:
        print(f"[microservicios] {nombre} está ENCENDIDO (antes estaba apagado o sin comprobar).")
        for funcion in list(_al_encender):
            threading.Thread(target=funcion, args=(nombre,), daemon=True).start()


def _en_pausa(nombre):
    with _candado_estado:
        info = _estado.get(nombre)
        return bool(info) and not info["activo"] and info["en_pausa_hasta"] > time.time()


def _es_fallo(respuesta):
    return respuesta.status_code >= 500 or respuesta.status_code in CODIGOS_DE_FALLO


def llamar(metodo, ruta, json=None, timeout=None, plazo_total=None):
    """
    Llama a `ruta` en el primer microservicio que responda bien.

    Devuelve una tupla (respuesta, nombre_del_servicio, fallos_previos):
      - respuesta: el objeto requests.Response del servicio que sí respondió.
      - nombre_del_servicio: "Python", "Java", "Node.js" o "Go".
      - fallos_previos: lista de textos con los servicios que fallaron antes,
        por ejemplo ["Python (HTTP 429)", "Java (ConnectTimeout)"].

    `timeout` es el máximo de espera POR servicio; `plazo_total` es el máximo
    para TODA la llamada (por defecto PLAZO_TOTAL_DEFECTO). Los servicios que
    fallaron hace pocos segundos se saltan, salvo que todos estén en esa
    situación (entonces se prueban todos igual).

    Lanza TodosFallaron si ninguno respondió.
    """
    if timeout is None:
        timeout = settings.MS_TIMEOUT_LECTURA
    if plazo_total is None:
        plazo_total = PLAZO_TOTAL_DEFECTO
    limite = time.time() + plazo_total

    configurados = [s for s in settings.MICROSERVICIOS if s["url"].strip()]
    candidatos = [s for s in configurados if not _en_pausa(s["nombre"])]
    if not candidatos:
        candidatos = configurados  # todos están "en pausa": probamos igual

    fallos = [f"{s['nombre']} (en pausa, falló hace poco)" for s in configurados if s not in candidatos]

    for servicio in candidatos:
        restante = limite - time.time()
        if restante <= 1:
            fallos.append(f"{servicio['nombre']} (sin tiempo)")
            continue

        base = servicio["url"].strip().rstrip("/")
        try:
            respuesta = requests.request(
                metodo, base + ruta, json=json,
                timeout=(TIMEOUT_CONEXION, min(timeout, restante)),
            )
        except requests.exceptions.RequestException as e:
            fallos.append(f"{servicio['nombre']} ({type(e).__name__})")
            print(f"[microservicios] {servicio['nombre']} no respondió: {type(e).__name__}")
            _marcar(servicio["nombre"], False)
            continue

        if _es_fallo(respuesta):
            fallos.append(f"{servicio['nombre']} (HTTP {respuesta.status_code})")
            # Estos datos ayudan a saber QUIÉN devolvió el error (Render, Cloudflare o el propio servicio).
            print(
                f"[microservicios] {servicio['nombre']} respondió {respuesta.status_code} "
                f"(server={respuesta.headers.get('server')}, "
                f"x-render-routing={respuesta.headers.get('x-render-routing')}): "
                f"{respuesta.text[:120]!r}"
            )
            _marcar(servicio["nombre"], False)
            continue

        _marcar(servicio["nombre"], True)
        return respuesta, servicio["nombre"], fallos

    # Nadie respondió: probablemente están dormidos. Los despertamos ya, así
    # el próximo intento (o la bandeja de sincronización) los encuentra listos.
    despertar_en_segundo_plano()
    raise TodosFallaron(fallos)


# ---------------------------------------------------------------------
# Estado y "despertar" de los microservicios
# ---------------------------------------------------------------------
# Los servicios gratis de Render se APAGAN tras ~15 minutos sin visitas. Al
# recibir una petición se vuelven a encender, pero tardan de 30 segundos a
# 2 minutos, y mientras arrancan Render responde 502/429/503. Por eso una
# petición normal a un servicio dormido "falla" y solo funciona si esperas y
# lo reintentas. OJO: en Render, una petición de un servicio a otro (Django ->
# microservicio) muchas veces NO logra despertarlo (devuelve 429/502 y el
# servicio ni se entera); la que sí lo despierta es la del NAVEGADOR (ver
# servicios/panel.html). Estas funciones sirven sobre todo para saber
# cuáles ya están listos.

def clave_de(servicio):
    """'Node.js' -> 'nodejs', 'Python' -> 'python' (para usar en URLs)."""
    return re.sub(r"\W", "", servicio["nombre"].lower())


def estado_servicio(servicio, timeout=8):
    """
    Hace GET a la ruta "/" del servicio. Esa misma petición es la que lo
    despierta si estaba dormido. Devuelve un dict con:
      activo: True si respondió 200 (ya está listo).
      detalle: "OK", "HTTP 502", "sin respuesta (ReadTimeout)", etc.
    """
    base = servicio["url"].strip().rstrip("/")
    resultado = {"clave": clave_de(servicio), "nombre": servicio["nombre"], "activo": False, "detalle": ""}
    if not base:
        resultado["detalle"] = "sin URL configurada"
        return resultado
    try:
        r = requests.get(base + "/", timeout=timeout)
    except requests.exceptions.RequestException as e:
        resultado["detalle"] = f"sin respuesta ({type(e).__name__})"
        _marcar(servicio["nombre"], False)
        return resultado
    resultado["activo"] = r.status_code == 200
    resultado["detalle"] = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
    # Si acaba de pasar de apagado a encendido, esto dispara la sincronización automática.
    _marcar(servicio["nombre"], resultado["activo"])
    return resultado


def estado_todos(clave="todos", timeout=8):
    """Comprueba (en paralelo) un servicio por su clave, o todos."""
    servicios = [s for s in settings.MICROSERVICIOS if clave in ("todos", clave_de(s))]
    if not servicios:
        return []
    with ThreadPoolExecutor(max_workers=len(servicios)) as pool:
        return list(pool.map(lambda s: estado_servicio(s, timeout), servicios))


def despertar_en_segundo_plano(timeout=90):
    """Manda un GET a los 4 servicios sin esperar la respuesta. Como Render
    enciende un servicio apenas le llega una petición, esto los despierta."""
    threading.Thread(target=estado_todos, kwargs={"timeout": timeout}, daemon=True).start()