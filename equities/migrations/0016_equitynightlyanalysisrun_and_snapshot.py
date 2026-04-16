from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equities", "0015_equityoptimizationrun_selected_sectors"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquityNightlyAnalysisRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("analysis_date", models.DateField(unique=True)),
                ("status", models.CharField(choices=[("pending", "Pendiente"), ("running", "En proceso"), ("completed", "Completada"), ("failed", "Fallida")], default="pending", max_length=16)),
                ("status_note", models.CharField(blank=True, max_length=255)),
                ("agent_provider", models.CharField(default="core", max_length=32)),
                ("agent_label", models.CharField(default="Analista nocturno", max_length=120)),
                ("summary_data", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-analysis_date", "-id"],
            },
        ),
        migrations.CreateModel(
            name="EquityNightlyAnalysisSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("analysis_date", models.DateField()),
                ("scope", models.CharField(choices=[("tracked", "Seguimiento guardado"), ("ibex", "Radar IBEX")], max_length=16)),
                ("analysis_key", models.CharField(max_length=80)),
                ("ticker", models.CharField(max_length=20)),
                ("quote_symbol", models.CharField(blank=True, max_length=40)),
                ("company_name", models.CharField(max_length=160)),
                ("status_key", models.CharField(blank=True, max_length=24)),
                ("sector_label", models.CharField(blank=True, max_length=120)),
                ("agent_provider", models.CharField(default="core", max_length=32)),
                ("analysis_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("position", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="nightly_analysis_snapshots", to="equities.equityposition")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="snapshots", to="equities.equitynightlyanalysisrun")),
            ],
            options={
                "ordering": ["scope", "company_name", "ticker"],
                "unique_together": {("run", "analysis_key")},
            },
        ),
    ]
