from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0007_active_device_name_constraint")]
    operations = [
        migrations.AlterField(
            model_name="operation",
            name="kind",
            field=models.CharField(
                max_length=32,
                choices=[
                    ("project_read", "Project read"),
                    ("project_apply_patch", "Project apply patch"),
                    ("project_shell", "Project shell"),
                    ("project_manage", "Project management"),
                    ("device_read", "Approved device read"),
                ],
            ),
        ),
    ]
