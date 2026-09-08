from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0002_device_unique_device_key_per_account")]

    operations = [
        migrations.RemoveConstraint(
            model_name="operation",
            name="unique_operation_idempotency_scope",
        ),
        migrations.AddConstraint(
            model_name="operation",
            constraint=models.UniqueConstraint(
                condition=~models.Q(idempotency_key=""),
                fields=(
                    "account",
                    "oauth_grant_id",
                    "device_link",
                    "device",
                    "project",
                    "kind",
                    "idempotency_key",
                ),
                name="unique_operation_idempotency_scope",
            ),
        ),
    ]
