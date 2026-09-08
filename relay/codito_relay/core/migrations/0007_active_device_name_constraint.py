from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0006_project_management_tool")]

    operations = [
        migrations.RemoveConstraint(
            model_name="device",
            name="unique_device_name_per_account",
        ),
        migrations.AddConstraint(
            model_name="device",
            constraint=models.UniqueConstraint(
                fields=("account", "name"),
                condition=models.Q(revoked_at__isnull=True),
                name="unique_active_device_name_per_account",
            ),
        ),
    ]
