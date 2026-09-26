import requests
from django.conf import settings
from django.shortcuts import render, redirect


def chat_ia(request):
    if request.method == "POST":
        pregunta = request.POST.get("pregunta", "").strip()

        if not pregunta:
            request.session["ia_error"] = "Escribe una pregunta antes de enviar."
            request.session.pop("ia_respuesta", None)
        else:
            try:
                resultado = requests.post(
                    settings.MICROSERVICIO_IA_URL,
                    json={"pregunta": pregunta},
                    timeout=20,
                )
                try:
                    datos = resultado.json()
                except ValueError:
                    datos = {}

                if resultado.ok and datos.get("respuesta"):
                    request.session["ia_respuesta"] = datos["respuesta"]
                    request.session.pop("ia_error", None)
                else:
                    request.session["ia_error"] = datos.get(
                        "error",
                        f"El microservicio respondió con error {resultado.status_code}.",
                    )
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

    return render(
        request,
        "ia/chat_ia.html",
        {"pregunta": pregunta, "respuesta": respuesta, "error": error},
    )