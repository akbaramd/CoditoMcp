from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0005_grant_family_and_operation_sequence")]

    operations = [
        migrations.AlterField(
            model_name="operation",
            name="kind",
            field=models.CharField(
                choices=[
                    ("project_read", "Project read"),
                    ("project_apply_patch", "Project apply patch"),
                    ("project_shell", "Project shell"),
                    ("project_manage", "Project management"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="operation",
            constraint=models.UniqueConstraint(
                condition=models.Q(project__isnull=True) & ~models.Q(idempotency_key=""),
                fields=(
                    "account",
                    "oauth_grant_id",
                    "device_link",
                    "device",
                    "kind",
                    "idempotency_key",
                ),
                name="unique_device_operation_idempotency_scope",
            ),
        ),
    ]
