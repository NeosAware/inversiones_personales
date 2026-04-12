from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0013_equityclosedposition_equityposition_opened_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="equityoptimizationrun",
            name="max_total_positions",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
