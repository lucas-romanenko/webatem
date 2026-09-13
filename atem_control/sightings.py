"""What a switcher or deck reports about itself, handed to the host.

The uploader and the consumer call these after a session; standalone
WebATEM has nowhere to keep a sighting (the default hooks drop it), a hosting
platform records it on the device's inventory row (``atem_control.hooks``).
"""
from atem_control import hooks


def record_video_mode(ip, label):
    return hooks.get().record_video_mode(ip, label)


def record_deck_model(ip, model):
    return hooks.get().record_deck_model(ip, model)
