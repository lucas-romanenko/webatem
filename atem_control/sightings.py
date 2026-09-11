"""Stand-in for upstream's ``av_equipment.atem_setup`` sightings.

Upstream records what a switcher reports about itself (its video mode, its
stored name, its model) on the switcher's inventory row; the synced uploader
calls ``record_video_mode`` after every session. There is no inventory in
this app, so the calls are accepted and dropped. Keeping the call sites
intact (rather than patching them out of the synced module) is what lets
``tools/sync_from_av_server.py`` copy the uploader verbatim.
"""


def record_video_mode(ip, label):
    """Accept the sighting; nothing to record it on."""
    return None
