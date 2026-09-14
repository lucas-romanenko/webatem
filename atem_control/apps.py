from django.apps import AppConfig


class AtemControlConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'atem_control'
    # The label is the app's identity in the database (table prefix,
    # migration history). It is NOT ``atem_control``: a platform that hosts
    # this app may already own an app of that name with its own history and
    # tables, and Django keys both on the label — two histories under one
    # label would apply the wrong migrations to the wrong tables.
    label = 'webatem_atem'

    def ready(self):
        # Register the Channels bridge with the media_pool sub-package's
        # on_watcher_created hook. Must happen before any watcher.acquire()
        # call — importing the module runs its top-level on_watcher_created()
        # registration as a side effect.
        from atem_control.media_pool import broadcast  # noqa: F401
