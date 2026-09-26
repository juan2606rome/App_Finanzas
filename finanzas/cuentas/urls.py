# ruta: finanzas/cuentas/urls.py
from django.urls import path
from . import views

app_name = "cuentas"

urlpatterns = [
    path("", views.index, name="index"),
    path("cuenta/nueva/", views.crear_cuenta, name="crear_cuenta"),
    path("cuenta/<int:cuenta_id>/", views.detalle_cuenta, name="detalle_cuenta"),
    path("cuenta/<int:cuenta_id>/editar/", views.editar_cuenta, name="editar_cuenta"),
    path("cuenta/<int:cuenta_id>/eliminar/", views.eliminar_cuenta, name="eliminar_cuenta"),
    path("cuenta/<int:cuenta_id>/depositar/", views.depositar, name="depositar"),
    path("cuenta/<int:cuenta_id>/retirar/", views.retirar, name="retirar"),
    path("transferir/", views.transferir, name="transferir"),
    path("historial/", views.historial_general, name="historial_general"),
]