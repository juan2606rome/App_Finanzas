from django.urls import path
from . import views

app_name = "ia"

urlpatterns = [
    path("", views.chat_ia, name="chat_ia"),
]