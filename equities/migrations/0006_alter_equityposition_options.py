from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0005_equityposition_position_kind_and_reference_profile"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="equityposition",
            options={"ordering": ["position_kind", "ticker"]},
        ),
    ]
