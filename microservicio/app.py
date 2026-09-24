import os

import requests
from flask_swagger_ui import get_swaggerui_blueprint
from flask import Flask, jsonify, request

app = Flask(__name__)

# configuracion para supabase , leer variables entorno de RENDER
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

# ruta principal que menciona que endpoint estan disponibles
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "servicio": "microservicio-tasa-cambio",
        "uso": "GET /api/tasa",
    })


# ruta principal para detectar y calcular tasa
@app.route("/api/tasa")
def obtener_tasa():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({
            "error": "falta configurar SUPABASE_URL y SUPABASE_KEY."
        }), 500
# error 500 error interno del servidor

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

    # hacer la peticion a supabase
    url = (
        f"{SUPABASE_URL}/rest/v1/tasas"
        "?select=*&order=fecha_actualizacion.desc&limit=1"
    )
    

# hacemos la peticion a supabase, tiempo limite 6 segundos
    try:
        respuesta = requests.get(url, headers=headers, timeout=6)
        respuesta.raise_for_status() #manejo de errrores
        filas = respuesta.json()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"No se pudo consultar Supabase: {e}"}), 502
    # error 502 bad gateway error de servidor

    if not filas:
        return jsonify({"error": "La tabla 'tasas' está vacía en Supabase."}), 404
    # error 404 no encontrado

    fila = filas[0] 
    # usamos lo de fila para poder acceder al primer elemento de la lista, en este caso usariamos la primera fila de la base de datos

    return jsonify({
        "tasa_usd_cop": fila["tasa_usd_cop"],
        "fecha_actualizacion": fila["fecha_actualizacion"],
    })


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
                    "Eres un asistente de soporte de una app de finanzas "
                    "personales que usa pesos colombianos (COP). Ayudas "
                    "con dudas generales de ahorro, tasas de cambio y uso "
                    "de la app. Responde en español, corto y claro."
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
        "description": "Microservicio que consulta la tasa USD a COP almacenada en Supabase y responde preguntas usando Mistral IA.",
        "version": "1.0.0"
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
                "summary": "Obtener la tasa USD a COP",
                "description": "Consulta en Supabase la tasa de cambio mas reciente.",
                "responses": {
                    "200": {
                        "description": "Tasa obtenida correctamente",
                        "content": {
                            "application/json": {
                                "example": {
                                    "tasa_usd_cop": 4343,
                                    "fecha_actualizacion": "2026-09-19T07:00:00"
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
                "summary": "Preguntarle algo a la IA",
                "description": "Envía una pregunta y devuelve la respuesta generada por Mistral IA.",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "example": {"pregunta": "¿Qué es una tasa de cambio?"}
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Respuesta generada correctamente",
                        "content": {
                            "application/json": {
                                "example": {
                                    "respuesta": "Una tasa de cambio es el valor al que se convierte una moneda en otra."
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