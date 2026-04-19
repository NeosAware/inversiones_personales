from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0017_equitypurchaseforecastbaseline"),
    ]

    operations = [
        migrations.AddField(
            model_name="equityoptimizationrun",
            name="selected_owned_tickers_applied",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="equityoptimizationrun",
            name="selected_owned_tickers",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
