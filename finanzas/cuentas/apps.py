# ruta: finanzas/cuentas/apps.py
import os
import sys

from django.apps import AppConfig

# Comandos de manage.py en los que NO debe arrancar el vigilante.
_COMANDOS_SIN_VIGILANTE = {
    "migrate", "makemigrations", "collectstatic", "check", "test",
    "shell", "createsuperuser", "showmigrations", "dbshell",
}


class CuentasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cuentas"

    def ready(self):
        # Con "manage.py <comando>" solo arrancamos para runserver (y solo en
        # el proceso que sirve, no en el que vigila los cambios de archivos).
        # Con gunicorn (Render) sys.argv[1] no es un comando de manage.py.
        comando = sys.argv[1] if len(sys.argv) > 1 else ""
        if comando in _COMANDOS_SIN_VIGILANTE:
            return
        if comando == "runserver" and os.environ.get("RUN_MAIN") != "true":
            return
        if os.environ.get("DESACTIVAR_VIGILANTE") == "1":
            return
        from . import supabase_sync
        supabase_sync.iniciar_vigilante()