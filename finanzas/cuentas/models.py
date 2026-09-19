from django.db import models


class Cuenta(models.Model):
    """Una cuenta o billetera: Nequi, Nu, Bancolombia, Efectivo, etc."""
    nombre = models.CharField(max_length=100)
    saldo = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

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
    cuenta_origen = models.ForeignKey(
        Cuenta, on_delete=models.CASCADE,
        related_name="movimientos_salida", null=True, blank=True,
    )
    cuenta_destino = models.ForeignKey(
        Cuenta, on_delete=models.CASCADE,
        related_name="movimientos_entrada", null=True, blank=True,
    )

    monto = models.DecimalField(max_digits=12, decimal_places=0)
    descripcion = models.CharField(max_length=255, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.get_tipo_display()} de ${self.monto}"

    def monto_formateado(self):
        """Lo mismo que saldo_formateado, pero para el monto del movimiento."""
        return f"{int(self.monto):,}".replace(",", ".")