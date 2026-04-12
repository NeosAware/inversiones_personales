from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0014_equityoptimizationrun_max_total_positions"),
    ]

    operations = [
        migrations.AddField(
            model_name="equityoptimizationrun",
            name="selected_sectors",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
