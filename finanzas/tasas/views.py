# ruta: finanzas/tasas/views.py
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.shortcuts import render

from cuentas.models import Cuenta


def cotizacion(request):
    """
    Esta vista consulta DOS fuentes de información:
    1) El modelo Cuenta (de la app "cuentas"), para saber cuánta
       plata tienes en total, en pesos.
    2) Un microservicio propio, desplegado en la nube, que devuelve
       la(s) tasa(s) de cambio consultando una base de datos distinta
       a la SQLite de este proyecto (Supabase).

    El microservicio devuelve una LISTA de monedas (no solo USD): si en
    la tabla "tasas" de Supabase hay una fila para "EUR" o cualquier
    otra, aparece aquí automáticamente, sin tocar código.
    """
    total_cop = sum((c.saldo for c in Cuenta.objects.all()), Decimal("0"))
    # total_cop es solo un número (no un objeto Cuenta), así que no
    # tiene el método saldo_formateado(). Lo formateamos aquí mismo,
    # con la misma fórmula del punto de miles.
    total_cop_formateado = f"{int(total_cop):,}".replace(",", ".")

    monedas = []
    error = None

    try:
        respuesta = requests.get(settings.MICROSERVICIO_TASA_URL, timeout=6)
        respuesta.raise_for_status()
        datos = respuesta.json()

        # Aceptamos dos formatos por si acaso: la lista nueva bajo la
        # clave "tasas", o (por compatibilidad) una sola tasa suelta con
        # la forma vieja {"tasa_usd_cop": ..., "fecha_actualizacion": ...}.
        filas = datos.get("tasas")
        if filas is None and "tasa_usd_cop" in datos:
            filas = [{
                "moneda": "USD",
                "tasa_cop": datos["tasa_usd_cop"],
                "fecha_actualizacion": datos.get("fecha_actualizacion"),
            }]
        filas = filas or []

        for fila in filas:
            try:
                tasa = Decimal(str(fila["tasa_cop"]))
            except (KeyError, InvalidOperation, TypeError):
                continue

            total_convertido = round(total_cop / tasa, 2) if tasa > 0 else None
            monedas.append({
                "moneda": fila.get("moneda", "?"),
                # Misma fórmula del punto de miles que usamos en cuentas:
                # redondeamos la tasa al peso más cercano para mostrarla.
                "tasa_formateada": f"{int(tasa):,}".replace(",", "."),
                "total_convertido": total_convertido,
                "fecha_actualizacion": fila.get("fecha_actualizacion"),
            })

        if not monedas:
            error = "El microservicio no devolvió ninguna tasa registrada."

    except requests.exceptions.RequestException as e:
        error = f"No se pudo contactar al microservicio: {e}"
    except (KeyError, ValueError, TypeError) as e:
        error = f"El microservicio respondió en un formato inesperado: {e}"

    return render(
        request,
        "tasas/cotizacion.html",
        {
            "total_cop": total_cop_formateado,
            "monedas": monedas,
            "error": error,
        },
    )