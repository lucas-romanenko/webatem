from django.apps import AppConfig


class AtemControlConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'atem_control'

    def ready(self):
        # Register the Channels bridge with the media_pool sub-package's
        # on_watcher_created hook. Must happen before any watcher.acquire()
        # call — importing the module runs its top-level on_watcher_created()
        # registration as a side effect.
        from atem_control.media_pool import broadcast  # noqa: F401
