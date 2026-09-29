# ruta: finanzas/servicios/views.py
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from cuentas import supabase_sync
from mysite.microservicios import estado_todos
from django.conf import settings
from mysite.microservicios import clave_de


def panel(request):
    """Pantalla 'Microservicios': estado de cada uno y botones para encenderlos."""
    supabase_sync.reanudar_si_hay_pendientes()
    servicios = [{"clave": clave_de(s), "nombre": s["nombre"]} for s in settings.MICROSERVICIOS]
    return render(request, "servicios/panel.html", {
        "servicios": servicios,
        "pendientes": supabase_sync.cantidad_pendientes(),
    })


def ping(request):
    """
    JSON con el estado de un servicio (?s=python) o de todos (?s=todos).
    El GET que hace por dentro es lo que DESPIERTA al servicio si dormía:
    la pantalla lo llama cada pocos segundos hasta que responde 'OK'.
    Además, si ya hay alguno encendido y quedaron operaciones sin subir a
    Supabase, se reanuda el envío automáticamente.
    """
    clave = request.GET.get("s", "todos")
    estados = estado_todos(clave)
    if any(e["activo"] for e in estados):
        supabase_sync.reanudar_si_hay_pendientes()
    return JsonResponse({"servicios": estados, "pendientes": supabase_sync.cantidad_pendientes()})


@require_POST
def sincronizar(request):
    """Botón 'Sincronizar todo': sube a Supabase todas las cuentas y movimientos."""
    cuentas, transacciones = supabase_sync.sync_todo()
    return JsonResponse({
        "cuentas": cuentas,
        "transacciones": transacciones,
        "pendientes": supabase_sync.cantidad_pendientes(),
    })