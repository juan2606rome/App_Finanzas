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

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

# Códigos 4xx que significan "este servicio no puede atenderte ahora".
CODIGOS_DE_FALLO = {408, 429}


class TodosFallaron(Exception):
    """Ninguno de los microservicios pudo atender la petición."""

    def __init__(self, fallos):
        self.fallos = fallos
        detalle = ", ".join(fallos) if fallos else "no hay ninguna URL de microservicio configurada"
        super().__init__(detalle)


def _es_fallo(respuesta):
    return respuesta.status_code >= 500 or respuesta.status_code in CODIGOS_DE_FALLO


def llamar(metodo, ruta, json=None, timeout=None):
    """
    Llama a `ruta` en el primer microservicio que responda bien.

    Devuelve una tupla (respuesta, nombre_del_servicio, fallos_previos):
      - respuesta: el objeto requests.Response del servicio que sí respondió.
      - nombre_del_servicio: "Python", "Java", "Node.js" o "Go".
      - fallos_previos: lista de textos con los servicios que fallaron antes,
        por ejemplo ["Python (HTTP 429)", "Java (ConnectTimeout)"].

    Lanza TodosFallaron si ninguno respondió.
    """
    if timeout is None:
        timeout = settings.MS_TIMEOUT_LECTURA

    fallos = []
    for servicio in settings.MICROSERVICIOS:
        base = servicio["url"].strip().rstrip("/")
        if not base:
            continue  # este servicio todavía no está configurado: se salta

        try:
            respuesta = requests.request(metodo, base + ruta, json=json, timeout=timeout)
        except requests.exceptions.RequestException as e:
            fallos.append(f"{servicio['nombre']} ({type(e).__name__})")
            print(f"[microservicios] {servicio['nombre']} no respondió: {type(e).__name__}")
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
            continue

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
        return resultado
    resultado["activo"] = r.status_code == 200
    resultado["detalle"] = "OK" if r.status_code == 200 else f"HTTP {r.status_code}"
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