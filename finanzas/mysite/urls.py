from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("cuentas.urls")),
    path("cotizacion/", include("tasas.urls")),
    path("chat-ia/", include("ia.urls")),
]