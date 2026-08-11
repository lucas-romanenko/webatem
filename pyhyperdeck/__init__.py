"""pyhyperdeck — Blackmagic HyperDeck Studio control library.

A small client for the HyperDeck Ethernet Protocol (TCP 9993, text /
line-oriented) plus an FTP file-upload helper. Studio HD Mini-class
units don't expose the HTTP REST API that the Plus/Pro/HDR SKUs got
in firmware 8.x, so this library targets the always-on 9993 + FTP
combination — works across the whole networked HyperDeck product line.

The protocol is documented in BMD's
``HyperDeckEthernetProtocol.pdf`` (December 2024).

Public surface::

    from pyhyperdeck import Hyperdeck, upload_clip

    # Control:
    with Hyperdeck('192.168.1.10') as hd:
        info = hd.device_info()        # dict
        clips = hd.disk_list()         # List[Clip]
        hd.stop()
        hd.clips_clear()
        hd.clips_add('my-clip.mp4')
        hd.play(loop=True, single_clip=True)

    # File upload:
    result = upload_clip('192.168.1.10', '/path/to/clip.mp4')
    print(result.throughput_mb_s)
"""

from pyhyperdeck.client import (
    Clip,
    Hyperdeck,
    HyperdeckError,
    Response,
)
from pyhyperdeck.upload import UploadResult, upload_clip

__all__ = [
    'Clip',
    'Hyperdeck',
    'HyperdeckError',
    'Response',
    'UploadResult',
    'upload_clip',
]
