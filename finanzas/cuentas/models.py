# ruta: finanzas/cuentas/models.py
from django.db import models


class Cuenta(models.Model):
    """Una cuenta o billetera: Nequi, Nu, Bancolombia, Efectivo, etc."""
    nombre = models.CharField(max_length=100)
    saldo = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    fecha_creacion = models.DateTimeField(auto_now_add=True)
    # Se actualiza solo cada vez que guardamos la cuenta (editar nombre,
    # depositar, retirar, transferir...). Sirve para saber cuándo fue el
    # último cambio y para la sincronización con Supabase.
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre

    def saldo_formateado(self):
        """Convierte 80000 en "80.000" (con punto, como en Colombia)."""
        return f"{int(self.saldo):,}".replace(",", ".")


class Transaccion(models.Model):
    """Un movimiento de dinero: depósito, retiro o transferencia."""
    # Mayúscula porque son valores fijos que nunca cambian (constantes).
    # Es solo una costumbre para que se distingan de una variable normal;
    # en minúscula funcionaría exactamente igual.
    DEPOSITO = "deposito"
    RETIRO = "retiro"
    TRANSFERENCIA = "transferencia"

    TIPO_CHOICES = [
        (DEPOSITO, "Depósito"),
        (RETIRO, "Retiro"),
        (TRANSFERENCIA, "Transferencia"),
    ]

    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)

    # En un depósito solo se usa cuenta_destino.
    # En un retiro solo se usa cuenta_origen.
    # En una transferencia se usan las dos.
    #
    # on_delete=SET_NULL (en vez de CASCADE): si el usuario elimina una
    # cuenta, el movimiento NO desaparece del historial, solo se queda
    # sin esa referencia. Así, si borras "Nu" después de haber
    # transferido de "Nequi" a "Nu", el historial de "Nequi" conserva
    # ese movimiento en vez de perderlo.
    cuenta_origen = models.ForeignKey(
        Cuenta, on_delete=models.SET_NULL,
        related_name="movimientos_salida", null=True, blank=True,
    )
    cuenta_destino = models.ForeignKey(
        Cuenta, on_delete=models.SET_NULL,
        related_name="movimientos_entrada", null=True, blank=True,
    )

    # "Fotos" del nombre de la cuenta en el momento del movimiento.
    # Se llenan solas al crear la Transaccion (ver save() más abajo) y
    # ya no cambian después. Gracias a esto, si más adelante borras o
    # renombras una cuenta, el historial sigue mostrando con qué cuenta
    # fue el movimiento, en vez de quedar en blanco.
    cuenta_origen_nombre = models.CharField(max_length=100, blank=True)
    cuenta_destino_nombre = models.CharField(max_length=100, blank=True)

    monto = models.DecimalField(max_digits=12, decimal_places=0)
    descripcion = models.CharField(max_length=255, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha"]

    def save(self, *args, **kwargs):
        # Solo la primera vez que se guarda (self.pk is None) copiamos el
        # nombre actual de las cuentas involucradas. Así queda fijo para
        # siempre, aunque luego la cuenta cambie de nombre o se elimine.
        if self.pk is None:
            if self.cuenta_origen_id and not self.cuenta_origen_nombre:
                self.cuenta_origen_nombre = self.cuenta_origen.nombre
            if self.cuenta_destino_id and not self.cuenta_destino_nombre:
                self.cuenta_destino_nombre = self.cuenta_destino.nombre
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.get_tipo_display()} de ${self.monto}"

    def monto_formateado(self):
        """Lo mismo que saldo_formateado, pero para el monto del movimiento."""
        return f"{int(self.monto):,}".replace(",", ".")