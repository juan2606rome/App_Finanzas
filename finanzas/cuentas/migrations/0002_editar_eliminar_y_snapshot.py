# ruta: finanzas/cuentas/migrations/0002_editar_eliminar_y_snapshot.py
# Generado a mano para acompañar los cambios de cuentas/models.py:
# - Cuenta.actualizado_en (para saber cuándo fue el último cambio)
# - Transaccion.cuenta_origen/cuenta_destino: CASCADE -> SET_NULL
#   (para no perder el historial al eliminar una cuenta)
# - Transaccion.cuenta_origen_nombre / cuenta_destino_nombre (foto del
#   nombre en el momento del movimiento)

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def rellenar_nombres_historicos(apps, schema_editor):
    """A las Transaccion que ya existían les copiamos el nombre actual
    de sus cuentas, para que el historial viejo también se vea bien."""
    Transaccion = apps.get_model('cuentas', 'Transaccion')
    for mov in Transaccion.objects.select_related('cuenta_origen', 'cuenta_destino'):
        cambiado = False
        if mov.cuenta_origen_id and not mov.cuenta_origen_nombre:
            mov.cuenta_origen_nombre = mov.cuenta_origen.nombre
            cambiado = True
        if mov.cuenta_destino_id and not mov.cuenta_destino_nombre:
            mov.cuenta_destino_nombre = mov.cuenta_destino.nombre
            cambiado = True
        if cambiado:
            mov.save(update_fields=['cuenta_origen_nombre', 'cuenta_destino_nombre'])


def revertir(apps, schema_editor):
    # No hace falta deshacer nada: son solo datos de respaldo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('cuentas', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='cuenta',
            name='actualizado_en',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='transaccion',
            name='cuenta_origen_nombre',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='transaccion',
            name='cuenta_destino_nombre',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AlterField(
            model_name='transaccion',
            name='cuenta_origen',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='movimientos_salida', to='cuentas.cuenta'),
        ),
        migrations.AlterField(
            model_name='transaccion',
            name='cuenta_destino',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='movimientos_entrada', to='cuentas.cuenta'),
        ),
        migrations.RunPython(rellenar_nombres_historicos, revertir),
    ]