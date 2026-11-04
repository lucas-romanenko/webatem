import sys

from django.apps import AppConfig

# Threads in this process take turns with the GIL at least this often (the
# interpreter's default is 5 ms). Every switcher session's UDP thread has to
# read and acknowledge its packets within milliseconds while other threads
# work (a still being decoded, a page being rendered): a switcher resends
# what it hasn't had acknowledged after ~80 ms, and a 4K still comes at line
# rate. With 5 ms turns an offline replay of a 4K Plus download beside one
# busy pure-Python thread never finished; with 1 ms it took 11 s
# (2026-11-04).
GIL_SWITCH_INTERVAL = 0.001


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
        if sys.getswitchinterval() > GIL_SWITCH_INTERVAL:
            sys.setswitchinterval(GIL_SWITCH_INTERVAL)
        # Register the Channels bridge with the media_pool sub-package's
        # on_watcher_created hook. Must happen before any watcher.acquire()
        # call — importing the module runs its top-level on_watcher_created()
        # registration as a side effect.
        from atem_control.media_pool import broadcast  # noqa: F401
