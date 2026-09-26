# ruta: finanzas/cuentas/views.py
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404

from . import supabase_sync
from .models import Cuenta, Transaccion


def index(request):
    """Pantalla principal: total de plata + lista de cuentas."""
    cuentas = Cuenta.objects.all()
    total = sum((c.saldo for c in cuentas), Decimal("0"))
    # El total no es un objeto Cuenta (es solo un número que sumamos
    # aquí), así que no tiene el método saldo_formateado(). Por eso
    # lo formateamos a mano, con la misma fórmula que usamos allá.
    total_formateado = f"{int(total):,}".replace(",", ".")

    return render(
        request,
        "cuentas/index.html",
        {"cuentas": cuentas, "total": total_formateado},
    )


def crear_cuenta(request):
    """Formulario para agregar una cuenta nueva."""
    if request.method == "POST":
        nombre = request.POST.get("nombre", "").strip()
        saldo_inicial = request.POST.get("saldo_inicial", "0").strip()

        try:
            saldo_inicial = Decimal(saldo_inicial or "0")
        except InvalidOperation:
            saldo_inicial = Decimal("0")

        if not nombre:
            messages.error(request, "El nombre de la cuenta es obligatorio.")
            return render(request, "cuentas/crear_cuenta.html")

        cuenta = Cuenta.objects.create(nombre=nombre, saldo=saldo_inicial)
        supabase_sync.sync_upsert_cuenta(cuenta)

        if saldo_inicial > 0:
            movimiento = Transaccion.objects.create(
                tipo=Transaccion.DEPOSITO,
                cuenta_destino=cuenta,
                monto=saldo_inicial,
                descripcion="Saldo inicial al crear la cuenta",
            )
            supabase_sync.sync_insertar_transaccion(movimiento)

        messages.success(request, f"Cuenta '{cuenta.nombre}' creada correctamente.")
        return redirect("cuentas:index")

    return render(request, "cuentas/crear_cuenta.html")


def editar_cuenta(request, cuenta_id):
    """Formulario para cambiar el nombre de una cuenta ya existente."""
    cuenta = get_object_or_404(Cuenta, pk=cuenta_id)

    if request.method == "POST":
        nuevo_nombre = request.POST.get("nombre", "").strip()

        if not nuevo_nombre:
            messages.error(request, "El nombre de la cuenta es obligatorio.")
            return render(request, "cuentas/editar_cuenta.html", {"cuenta": cuenta})

        cuenta.nombre = nuevo_nombre
        cuenta.save()
        supabase_sync.sync_upsert_cuenta(cuenta)

        messages.success(request, f"La cuenta ahora se llama '{cuenta.nombre}'.")
        return redirect("cuentas:detalle_cuenta", cuenta_id=cuenta.id)

    return render(request, "cuentas/editar_cuenta.html", {"cuenta": cuenta})


def eliminar_cuenta(request, cuenta_id):
    """
    Confirma y elimina una cuenta.

    Si la cuenta todavía tiene saldo, el propio template le muestra al
    usuario una advertencia fuerte (cuánto dinero va a "perder de
    vista"), pero igual lo dejamos confirmar si de verdad quiere
    borrarla: los movimientos no se borran (ver Transaccion con
    on_delete=SET_NULL), solo la cuenta como tal.
    """
    cuenta = get_object_or_404(Cuenta, pk=cuenta_id)

    if request.method == "POST":
        nombre = cuenta.nombre
        cuenta_id_borrado = cuenta.id
        cuenta.delete()
        supabase_sync.sync_eliminar_cuenta(cuenta_id_borrado)

        messages.success(request, f"Se eliminó la cuenta '{nombre}'.")
        return redirect("cuentas:index")

    return render(request, "cuentas/eliminar_cuenta.html", {"cuenta": cuenta})


def detalle_cuenta(request, cuenta_id):
    """
    Ruta dinámica: recibe el id de una cuenta por la URL y muestra
    su saldo y su historial de movimientos.
    """
    cuenta = get_object_or_404(Cuenta, pk=cuenta_id)

    # Una cuenta puede aparecer en un movimiento de dos formas
    # distintas: como origen (salió plata de ahí) o como destino
    # (entró plata ahí). Como son dos preguntas distintas, las
    # separamos en dos consultas normales con .filter(), y luego
    # las juntamos en una sola lista ordenada por fecha (la más
    # nueva primero).
    salidas = Transaccion.objects.filter(cuenta_origen=cuenta)
    entradas = Transaccion.objects.filter(cuenta_destino=cuenta)
    movimientos = sorted(
        list(salidas) + list(entradas),
        key=lambda mov: mov.fecha,
        reverse=True,
    )

    return render(
        request,
        "cuentas/detalle_cuenta.html",
        {"cuenta": cuenta, "movimientos": movimientos},
    )


def depositar(request, cuenta_id):
    """Ruta dinámica: agrega dinero a la cuenta indicada por cuenta_id."""
    cuenta = get_object_or_404(Cuenta, pk=cuenta_id)

    if request.method == "POST":
        try:
            monto = Decimal(request.POST.get("monto", "0"))
        except InvalidOperation:
            monto = Decimal("0")

        if monto <= 0:
            messages.error(request, "El monto debe ser mayor a cero.")
        else:
            cuenta.saldo += monto
            cuenta.save()
            supabase_sync.sync_upsert_cuenta(cuenta)

            movimiento = Transaccion.objects.create(
                tipo=Transaccion.DEPOSITO,
                cuenta_destino=cuenta,
                monto=monto,
                descripcion=request.POST.get("descripcion", ""),
            )
            supabase_sync.sync_insertar_transaccion(movimiento)

            messages.success(request, f"Se agregaron ${monto} a {cuenta.nombre}.")
            return redirect("cuentas:detalle_cuenta", cuenta_id=cuenta.id)

    return render(request, "cuentas/depositar.html", {"cuenta": cuenta})


def retirar(request, cuenta_id):
    """Ruta dinámica: retira dinero de la cuenta indicada por cuenta_id."""
    cuenta = get_object_or_404(Cuenta, pk=cuenta_id)

    if request.method == "POST":
        try:
            monto = Decimal(request.POST.get("monto", "0"))
        except InvalidOperation:
            monto = Decimal("0")

        if monto <= 0:
            messages.error(request, "El monto debe ser mayor a cero.")
        elif monto > cuenta.saldo:
            messages.error(
                request,
                f"No puedes retirar ${monto}: en {cuenta.nombre} solo hay ${cuenta.saldo}.",
            )
        else:
            cuenta.saldo -= monto
            cuenta.save()
            supabase_sync.sync_upsert_cuenta(cuenta)

            movimiento = Transaccion.objects.create(
                tipo=Transaccion.RETIRO,
                cuenta_origen=cuenta,
                monto=monto,
                descripcion=request.POST.get("descripcion", ""),
            )
            supabase_sync.sync_insertar_transaccion(movimiento)

            messages.success(request, f"Se retiraron ${monto} de {cuenta.nombre}.")
            return redirect("cuentas:detalle_cuenta", cuenta_id=cuenta.id)

    return render(request, "cuentas/retirar.html", {"cuenta": cuenta})


def transferir(request):
    """Mueve dinero de una cuenta propia a otra. Ej: Nequi -> Nu."""
    cuentas = Cuenta.objects.all()

    if request.method == "POST":
        origen_id = request.POST.get("origen")
        destino_id = request.POST.get("destino")
        try:
            monto = Decimal(request.POST.get("monto", "0"))
        except InvalidOperation:
            monto = Decimal("0")

        origen = get_object_or_404(Cuenta, pk=origen_id)
        destino = get_object_or_404(Cuenta, pk=destino_id)

        if origen.id == destino.id:
            messages.error(request, "La cuenta origen y destino no pueden ser la misma.")
        elif monto <= 0:
            messages.error(request, "El monto debe ser mayor a cero.")
        elif monto > origen.saldo:
            messages.error(
                request,
                f"No puedes transferir ${monto}: en {origen.nombre} solo hay ${origen.saldo}.",
            )
        else:
            origen.saldo -= monto
            destino.saldo += monto
            origen.save()
            destino.save()
            supabase_sync.sync_upsert_cuenta(origen)
            supabase_sync.sync_upsert_cuenta(destino)

            movimiento = Transaccion.objects.create(
                tipo=Transaccion.TRANSFERENCIA,
                cuenta_origen=origen,
                cuenta_destino=destino,
                monto=monto,
                descripcion=request.POST.get("descripcion", ""),
            )
            supabase_sync.sync_insertar_transaccion(movimiento)

            messages.success(
                request, f"Se transfirieron ${monto} de {origen.nombre} a {destino.nombre}."
            )
            return redirect("cuentas:index")

    return render(request, "cuentas/transferir.html", {"cuentas": cuentas})


def historial_general(request):
    """Todas las transacciones de todas las cuentas, más recientes primero."""
    movimientos = Transaccion.objects.all()

    return render(
        request,
        "cuentas/historial_general.html",
        {"movimientos": movimientos},
    )