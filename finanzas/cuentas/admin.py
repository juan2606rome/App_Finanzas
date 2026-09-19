from django.contrib import admin
from .models import Cuenta, Transaccion

admin.site.register(Cuenta)
admin.site.register(Transaccion)