from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0004_equityposition_annual_maintenance_cost_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="equityposition",
            name="position_kind",
            field=models.CharField(
                choices=[("owned", "Comprada"), ("watchlist", "En seguimiento")],
                default="owned",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="equityposition",
            name="reference_profile",
            field=models.CharField(
                choices=[
                    ("market_index", "Indice o activo cotizado"),
                    ("euribor_12m", "Euribor 12 meses"),
                    ("spain_house_price", "Precio vivienda Espana"),
                ],
                default="market_index",
                max_length=24,
            ),
        ),
    ]
