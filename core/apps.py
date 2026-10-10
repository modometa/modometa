from django.apps import AppConfig
from django.conf import settings
from django.db.backends.signals import connection_created


def configure_sqlite_pragmas(sender, connection, **kwargs):
    """Apply high-performance read and memory caching PRAGMAs to SQLite connections."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA mmap_size = 268435456;")
        cursor.execute("PRAGMA cache_size = -32000;")
        cursor.execute("PRAGMA temp_store = MEMORY;")
        if getattr(settings, "DATABASE_READ_ONLY", False):
            cursor.execute("PRAGMA query_only = ON;")


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "Modometa Core"

    def ready(self) -> None:
        connection_created.connect(configure_sqlite_pragmas)
