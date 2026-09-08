from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0003_scope_operation_idempotency")]

    operations = [
        migrations.AddField(
            model_name="operation",
            name="request_payload",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="operation",
            name="delivery_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="operation",
            name="sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="operation",
            name="sent_epoch",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
