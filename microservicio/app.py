import os

import requests
from flask import Flask, jsonify

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "servicio": "microservicio-tasa-cambio",
        "uso": "GET /api/tasa",
    })


Python
@app.route("/api/tasa")
def obtener_tasa():
    """
    Consulta en Supabase la fila más reciente de la tabla "tasas"
    usando la API REST que Supabase genera automáticamente, y la
    devuelve como JSON.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({
            "error": "Falta configurar SUPABASE_URL y SUPABASE_KEY."
        }), 500

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }

    # Limpiamos la URL por si incluye /rest/v1/ al final
    base_url = SUPABASE_URL.rstrip("/")
    if base_url.endswith("/rest/v1"):
        base_url = base_url[:-8]  # Le quita el /rest/v1 si lo trae

    url = (
        f"{base_url}/rest/v1/tasas"
        "?select=*&order=fecha_actualizacion.desc&limit=1"
    )

    try:
        respuesta = requests.get(url, headers=headers, timeout=6)
        respuesta.raise_for_status()
        filas = respuesta.json()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"No se pudo consultar Supabase: {e}"}), 502

    if not filas:
        return jsonify({"error": "La tabla 'tasas' está vacía en Supabase."}), 404

    fila = filas[0]
    return jsonify({
        "tasa_usd_cop": fila["tasa_usd_cop"],
        "fecha_actualizacion": fila["fecha_actualizacion"],
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)