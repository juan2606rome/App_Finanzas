# ruta: microservicio/app.py
import os
from datetime import datetime

import requests
from flask_swagger_ui import get_swaggerui_blueprint
from flask import Flask, jsonify, request

app = Flask(__name__)

# configuracion para supabase , leer variables entorno de RENDER
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

# Cuántos movimientos recientes le pasamos a la IA como contexto.
# Si lo subes mucho, la pregunta a Mistral se vuelve más pesada (y más
# cara/lenta); con 15-20 alcanza para responder "cuáles fueron mis
# últimas transferencias" con margen de sobra.
MAX_MOVIMIENTOS_CONTEXTO = 15


def _supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def _consultar_supabase(tabla, query):
    """
    GET genérico a una tabla de Supabase vía su API REST (PostgREST).
    Devuelve la lista de filas, o lanza requests.exceptions.RequestException
    si algo falla (falta de red, tabla no existe, etc.).
    """
    url = f"{SUPABASE_URL}/rest/v1/{tabla}?{query}"
    respuesta = requests.get(url, headers=_supabase_headers(), timeout=6)
    respuesta.raise_for_status()
    return respuesta.json()


def _formatear_pesos(valor):
    try:
        return f"{int(round(float(valor))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(valor)


# ruta principal que menciona que endpoint estan disponibles
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "servicio": "microservicio-tasa-cambio",
        "uso": "GET /api/tasa, POST /api/preguntar",
    })


# ruta principal para detectar y calcular tasa(s)
@app.route("/api/tasa")
def obtener_tasa():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({
            "error": "falta configurar SUPABASE_URL y SUPABASE_KEY."
        }), 500
    # error 500 error interno del servidor

    # Traemos TODAS las filas de la tabla "tasas" (no solo una): así, si
    # el usuario agrega una moneda nueva en Supabase (por ejemplo EUR),
    # aparece aquí automáticamente sin tocar este código.
    try:
        filas = _consultar_supabase("tasas", "select=*&order=moneda.asc")
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"No se pudo consultar Supabase: {e}"}), 502
    # error 502 bad gateway error de servidor

    if not filas:
        return jsonify({"error": "La tabla 'tasas' está vacía en Supabase."}), 404
    # error 404 no encontrado

    tasas = [
        {
            "moneda": fila.get("moneda"),
            "tasa_cop": fila.get("tasa_cop"),
            "fecha_actualizacion": fila.get("fecha_actualizacion"),
        }
        for fila in filas
        if fila.get("moneda") and fila.get("tasa_cop") is not None
    ]

    return jsonify({"tasas": tasas})


def _construir_contexto_financiero():
    """
    Arma un bloque de texto en español con la "foto" actual de las
    finanzas del usuario, leyendo Supabase (que Django mantiene
    sincronizado con la base local). Esto es lo que le pasamos a
    Mistral como contexto para que pueda responder preguntas
    específicas ("¿cuántos dólares puedo tener?", "¿cuáles fueron mis
    últimas transferencias?").

    Si algo falla (Supabase caído, tablas vacías, etc.) devolvemos un
    texto explicándolo, para que la IA no invente datos y en cambio
    le diga al usuario que no pudo consultar su información.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "(No hay conexión configurada a la base de datos del usuario.)"

    partes = []

    # --- Cuentas y total ---
    try:
        cuentas = _consultar_supabase("cuentas", "select=nombre,saldo,actualizado_en&order=nombre.asc")
    except requests.exceptions.RequestException:
        cuentas = None

    if cuentas is None:
        partes.append("No se pudieron leer las cuentas del usuario en este momento.")
    elif not cuentas:
        partes.append("El usuario todavía no tiene ninguna cuenta creada.")
    else:
        total_cop = sum(float(c.get("saldo") or 0) for c in cuentas)
        lineas = [f"- {c['nombre']}: ${_formatear_pesos(c['saldo'])} COP" for c in cuentas]
        partes.append(
            "Cuentas actuales del usuario (en pesos colombianos, COP):\n"
            + "\n".join(lineas)
            + f"\nTotal en todas las cuentas: ${_formatear_pesos(total_cop)} COP"
        )

    # --- Tasas de cambio, y equivalente del total en cada moneda ---
    try:
        tasas = _consultar_supabase("tasas", "select=*&order=moneda.asc")
    except requests.exceptions.RequestException:
        tasas = None

    if tasas:
        total_cop_num = sum(float(c.get("saldo") or 0) for c in (cuentas or []))
        lineas = []
        for t in tasas:
            moneda = t.get("moneda")
            tasa_cop = t.get("tasa_cop")
            if not moneda or not tasa_cop:
                continue
            try:
                equivalente = round(total_cop_num / float(tasa_cop), 2)
                lineas.append(
                    f"- 1 {moneda} = ${_formatear_pesos(tasa_cop)} COP "
                    f"(el total del usuario equivale a ~{equivalente} {moneda})"
                )
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if lineas:
            partes.append("Tasas de cambio actuales:\n" + "\n".join(lineas))

    # --- Últimos movimientos ---
    try:
        movimientos = _consultar_supabase(
            "transacciones",
            f"select=*&order=fecha.desc&limit={MAX_MOVIMIENTOS_CONTEXTO}",
        )
    except requests.exceptions.RequestException:
        movimientos = None

    if movimientos is None:
        partes.append("No se pudo leer el historial de movimientos en este momento.")
    elif not movimientos:
        partes.append("El usuario todavía no tiene movimientos registrados.")
    else:
        lineas = []
        for m in movimientos:
            fecha = m.get("fecha", "")
            try:
                fecha = datetime.fromisoformat(fecha.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
            except (ValueError, AttributeError):
                pass

            tipo = m.get("tipo")
            monto = _formatear_pesos(m.get("monto"))
            origen = m.get("cuenta_origen_nombre") or "(cuenta eliminada)"
            destino = m.get("cuenta_destino_nombre") or "(cuenta eliminada)"

            if tipo == "transferencia":
                detalle = f"transferencia de ${monto} de {origen} a {destino}"
            elif tipo == "deposito":
                detalle = f"depósito de ${monto} en {destino}"
            elif tipo == "retiro":
                detalle = f"retiro de ${monto} de {origen}"
            else:
                detalle = f"{tipo} de ${monto}"

            lineas.append(f"- {fecha}: {detalle}")

        partes.append(
            f"Últimos {len(lineas)} movimientos del usuario (el primero es el más reciente):\n"
            + "\n".join(lineas)
        )

    return "\n\n".join(partes)


# ruta que recibe una pregunta y la responde usando Mistral IA
@app.route("/api/preguntar", methods=["POST"])
def preguntar_ia():
    if not MISTRAL_API_KEY:
        return jsonify({
            "error": "Falta configurar MISTRAL_API_KEY."
        }), 500
    # error 500 error interno del servidor

    datos = request.get_json(silent=True) or {}
    pregunta = (datos.get("pregunta") or "").strip()

    if not pregunta:
        return jsonify({"error": "No se recibió ninguna pregunta."}), 400
    # error 400 solicitud incorrecta (el cliente no mandó nada útil)

    contexto = _construir_contexto_financiero()

    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }

    cuerpo = {
        "model": "ministral-8b-2512",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Eres el asistente de una app de finanzas personales que "
                    "usa pesos colombianos (COP). A continuación tienes la "
                    "información real y actualizada de las cuentas, tasas de "
                    "cambio y movimientos del usuario. Úsala para responder "
                    "sus preguntas con datos exactos (por ejemplo, cuánto "
                    "dinero tiene en total, a cuántos dólares equivale, o "
                    "cuáles fueron sus últimos movimientos). Si la pregunta "
                    "no tiene que ver con esta información, respóndela igual "
                    "con tus conocimientos generales. Si no encuentras el "
                    "dato exacto que te piden en la información de abajo, "
                    "dilo claramente en vez de inventarlo. Responde en "
                    "español, corto y claro.\n\n"
                    "=== INFORMACIÓN ACTUAL DEL USUARIO ===\n"
                    f"{contexto}"
                ),
            },
            {"role": "user", "content": pregunta},
        ],
        "temperature": 0.3,
    }

    # hacemos la peticion a Mistral, tiempo limite 15 segundos
    try:
        respuesta = requests.post(MISTRAL_URL, headers=headers, json=cuerpo, timeout=15)
        respuesta.raise_for_status()
        datos_respuesta = respuesta.json()
        texto = datos_respuesta["choices"][0]["message"]["content"]
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"No se pudo contactar a Mistral: {e}"}), 502
        # error 502 bad gateway, error del servidor externo
    except (KeyError, IndexError, ValueError) as e:
        return jsonify({"error": f"Respuesta inesperada de Mistral: {e}"}), 502

    return jsonify({"respuesta": texto})


# =========================
# CONFIGURACION DE SWAGGER
# =========================

SWAGGER_URL = "/docs"
API_URL = "/swagger.json"

swaggerui_blueprint = get_swaggerui_blueprint(
    SWAGGER_URL,
    API_URL,
    config={
        "app_name": "Microservicio Tasa de Cambio"
    }
)

app.register_blueprint(swaggerui_blueprint)


# Documentacion Swagger
SWAGGER_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Microservicio Tasa de Cambio",
        "description": (
            "Microservicio que consulta en Supabase las tasas de cambio "
            "(una o varias monedas), las cuentas y el historial de "
            "movimientos, y responde preguntas usando Mistral IA con ese "
            "contexto."
        ),
        "version": "2.0.0"
    },
    "paths": {
        "/": {
            "get": {
                "summary": "Estado del microservicio",
                "responses": {
                    "200": {
                        "description": "Servicio funcionando correctamente"
                    }
                }
            }
        },
        "/api/tasa": {
            "get": {
                "summary": "Obtener las tasas de cambio registradas",
                "description": "Consulta en Supabase todas las filas de la tabla 'tasas' (USD, EUR, etc.).",
                "responses": {
                    "200": {
                        "description": "Tasas obtenidas correctamente",
                        "content": {
                            "application/json": {
                                "example": {
                                    "tasas": [
                                        {"moneda": "USD", "tasa_cop": 4300, "fecha_actualizacion": "2026-09-19T07:00:00"},
                                        {"moneda": "EUR", "tasa_cop": 4700, "fecha_actualizacion": "2026-09-19T07:00:00"}
                                    ]
                                }
                            }
                        }
                    },
                    "404": {
                        "description": "No hay datos en la tabla tasas"
                    },
                    "500": {
                        "description": "Faltan las variables de Supabase"
                    },
                    "502": {
                        "description": "Error al consultar Supabase"
                    }
                }
            }
        },
        "/api/preguntar": {
            "post": {
                "summary": "Preguntarle algo a la IA sobre tus finanzas",
                "description": "Envía una pregunta; el microservicio arma contexto desde Supabase (cuentas, tasas, movimientos) y devuelve la respuesta generada por Mistral IA.",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "example": {"pregunta": "¿Cuántos dólares puedo tener con mi plata?"}
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Respuesta generada correctamente",
                        "content": {
                            "application/json": {
                                "example": {
                                    "respuesta": "Con tu total en pesos, hoy tendrías aproximadamente 395.35 USD."
                                }
                            }
                        }
                    },
                    "400": {
                        "description": "No se envió ninguna pregunta"
                    },
                    "500": {
                        "description": "Falta configurar MISTRAL_API_KEY"
                    },
                    "502": {
                        "description": "Error al consultar Mistral"
                    }
                }
            }
        }
    }
}


# Ruta que entrega la documentacion Swagger en JSON
@app.route("/swagger.json")
def swagger_json():
    return jsonify(SWAGGER_SPEC)


if __name__ == "__main__":
    app.run(debug=True, port=5000)