from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "codito_relay.core"
    verbose_name = "Codito"

    def ready(self) -> None:
        from . import signals  # noqa: F401
