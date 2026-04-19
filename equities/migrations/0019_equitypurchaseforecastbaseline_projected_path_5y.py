from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0018_equityoptimizationrun_selected_owned_tickers"),
    ]

    operations = [
        migrations.AddField(
            model_name="equitypurchaseforecastbaseline",
            name="projected_path_5y",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
