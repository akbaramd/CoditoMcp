from __future__ import annotations

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_operation_delivery_state"),
        ("oauth2_provider", "0022_refreshtoken_token_family_index"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OAuthGrantFamily",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("token_family", models.UUIDField(db_index=True, unique=True)),
                ("resource", models.URLField(max_length=500)),
                ("scopes_digest", models.CharField(max_length=64)),
                ("oauth_grant_id", models.CharField(db_index=True, max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="oauth2_provider.application",
                    ),
                ),
                (
                    "device_link",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="oauth_grant_families",
                        to="core.devicelink",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="operation",
            name="last_device_sequence",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="operation",
            name="last_device_sequence_epoch",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
