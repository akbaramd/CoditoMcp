from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0012_explicit_full_access_mode")]

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
                    ("project_code", "Project code intelligence"),
                    ("device_read", "Approved device read"),
                    ("device_screenshot", "Approved screenshot"),
                    ("device_desktop", "Approved desktop action"),
                ],
                max_length=32,
            ),
        ),
    ]
