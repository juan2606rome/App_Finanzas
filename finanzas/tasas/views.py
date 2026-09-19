from decimal import Decimal

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
       la tasa de cambio del dólar consultando una base de datos
       distinta a la SQLite de este proyecto (Supabase).
    """
    total_cop = sum((c.saldo for c in Cuenta.objects.all()), Decimal("0"))
    # total_cop es solo un número (no un objeto Cuenta), así que no
    # tiene el método saldo_formateado(). Lo formateamos aquí mismo,
    # con la misma fórmula del punto de miles.
    total_cop_formateado = f"{int(total_cop):,}".replace(",", ".")

    tasa_formateada = None
    total_usd = None  # este SÍ se queda en dólares, no lo formateamos como pesos
    error = None
    fecha_actualizacion = None

    try:
        respuesta = requests.get(settings.MICROSERVICIO_TASA_URL, timeout=6)
        respuesta.raise_for_status()
        datos = respuesta.json()

        tasa = Decimal(str(datos["tasa_usd_cop"]))
        tasa_formateada = f"{int(tasa):,}".replace(",", ".")
        fecha_actualizacion = datos.get("fecha_actualizacion")

        if tasa > 0:
            total_usd = round(total_cop / tasa, 2)

    except requests.exceptions.RequestException as e:
        error = f"No se pudo contactar al microservicio: {e}"
    except (KeyError, ValueError, TypeError) as e:
        error = f"El microservicio respondió en un formato inesperado: {e}"

    return render(
        request,
        "tasas/cotizacion.html",
        {
            "total_cop": total_cop_formateado,
            "tasa": tasa_formateada,
            "total_usd": total_usd,
            "fecha_actualizacion": fecha_actualizacion,
            "error": error,
        },
    )