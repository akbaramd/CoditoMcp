from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0011_desktop_actions")]

    # Choice expansion only: never upgrade old shell-trusted projects to full
    # screen/file/browser authority without a new explicit local selection.
    operations = [
        migrations.AlterField(
            model_name="project",
            name="mode",
            field=models.CharField(
                choices=[
                    ("isolated", "Isolated"),
                    ("native_approval", "Native approval"),
                    ("native_trusted", "Native trusted"),
                    ("full_access", "Full access (explicit local consent)"),
                    ("native_project", "Project access (native)"),
                ],
                default="isolated",
                max_length=24,
            ),
        ),
    ]
