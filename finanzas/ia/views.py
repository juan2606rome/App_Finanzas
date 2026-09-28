# ruta: finanzas/ia/views.py
import requests
from django.shortcuts import render, redirect

from mysite.microservicios import TodosFallaron, llamar


def chat_ia(request):
    if request.method == "POST":
        pregunta = request.POST.get("pregunta", "").strip()

        if not pregunta:
            request.session["ia_error"] = "Escribe una pregunta antes de enviar."
            request.session.pop("ia_respuesta", None)
        else:
            try:
                # Resiliencia: prueba Python -> Java -> Node.js -> Go
                # (ver mysite/microservicios.py). Solo se pasa al siguiente si
                # el anterior no responde o falla con error del servidor.
                resultado, servicio, fallos = llamar(
                    "POST", "/api/preguntar", json={"pregunta": pregunta}, timeout=25
                )
                try:
                    datos = resultado.json()
                except ValueError:
                    datos = {}

                if resultado.ok and datos.get("respuesta"):
                    request.session["ia_respuesta"] = datos["respuesta"]
                    request.session["ia_servicio"] = servicio
                    request.session["ia_fallos"] = fallos
                    request.session.pop("ia_error", None)
                else:
                    request.session["ia_error"] = datos.get(
                        "error",
                        f"El microservicio {servicio} respondió con error {resultado.status_code}.",
                    )
                    request.session.pop("ia_respuesta", None)
            except TodosFallaron as e:
                request.session["ia_error"] = f"No respondió ningún microservicio de IA ({e})."
                request.session.pop("ia_respuesta", None)
            except requests.exceptions.RequestException as e:
                request.session["ia_error"] = f"No se pudo contactar al microservicio de IA: {e}"
                request.session.pop("ia_respuesta", None)

            request.session["ia_pregunta"] = pregunta

        # Clave: redirigimos a la misma vista en vez de renderizar directo.
        # Así el navegador queda parado en un GET, y si el usuario recarga
        # (F5), solo repite el GET (que no le manda nada a la IA), en vez
        # de repetir el POST y volver a gastar una llamada a Mistral.
        return redirect("ia:chat_ia")

    # Si llegamos aquí es por GET (ya sea la primera vez que entras a la
    # página, o justo después de la redirección de arriba).
    # .pop() lee el valor Y lo borra de la sesión, para que si recargas
    # otra vez, ya no vuelva a aparecer la misma respuesta de nuevo.
    pregunta = request.session.pop("ia_pregunta", "")
    respuesta = request.session.pop("ia_respuesta", None)
    error = request.session.pop("ia_error", None)
    servicio = request.session.pop("ia_servicio", None)
    fallos = request.session.pop("ia_fallos", [])

    return render(
        request,
        "ia/chat_ia.html",
        {
            "pregunta": pregunta,
            "respuesta": respuesta,
            "error": error,
            "servicio": servicio,
            "fallos": fallos,
        },
    )