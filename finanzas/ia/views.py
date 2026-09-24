import requests
from django.conf import settings
from django.shortcuts import render


def chat_ia(request):
    """
    Chat de una sola pregunta y una sola respuesta (sin historial).
    Le pasa la pregunta del usuario a nuestro microservicio, que a su
    vez se la manda a Mistral, y muestra lo que responda.
    """
    pregunta = ""
    respuesta = None
    error = None

    if request.method == "POST":
        pregunta = request.POST.get("pregunta", "").strip()

        if not pregunta:
            error = "Escribe una pregunta antes de enviar."
        else:
            try:
                resultado = requests.post(
                    settings.MICROSERVICIO_IA_URL,
                    json={"pregunta": pregunta},
                    timeout=15,
                )
                resultado.raise_for_status()
                datos = resultado.json()
                respuesta = datos.get("respuesta")

                if not respuesta:
                    error = datos.get(
                        "error", "El microservicio no devolvió una respuesta."
                    )
            except requests.exceptions.RequestException as e:
                error = f"No se pudo contactar al microservicio de IA: {e}"

    return render(
        request,
        "ia/chat_ia.html",
        {"pregunta": pregunta, "respuesta": respuesta, "error": error},
    )