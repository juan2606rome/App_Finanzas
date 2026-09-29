# ruta: finanzas/servicios/urls.py
from django.urls import path
from . import views

app_name = "servicios"

urlpatterns = [
    path("", views.panel, name="panel"),
    path("ping/", views.ping, name="ping"),
    path("sincronizar/", views.sincronizar, name="sincronizar"),
]