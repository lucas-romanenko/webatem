"""A saved profile's ZIP and its XML agree on every still's file, and a
restore gives every slot its own picture back under its own name.

The XML points each slot at ``ATEM Media Pool/<name>.png``; a restore (ours,
ATEM Software Control's) finds the picture by that path. Before, the ZIP
named files its own way: the same picture under two names was written once
(the second slot's file was missing), two pictures under one name became
``name.png`` and ``name_2.png`` while the XML sent both slots to the first
(the second restored the wrong picture), and an unnamed slot's file had no
path at all. And the restore took a file out of play once one slot had used
it, so slots sharing a picture lost all but the first.
"""
import io
import json
import zipfile
import xml.etree.ElementTree as ET

from PIL import Image

from atemwire import Profile

from atem_control.profile import export, views


def _png(colour) -> bytes:
    buf = io.BytesIO()
    Image.new('RGBA', (8, 4), colour).save(buf, format='PNG')
    return buf.getvalue()


P1, P2, P3, P4, P5 = (_png((n * 40, 10, 10, 255)) for n in range(1, 6))

# slot: (name the switcher reports, picture)
POOL = {
    0: ('Logo', P1),
    1: ('Logo copy', P1),          # the same picture under another name
    2: ('Background', P2),
    3: ('Background', P3),         # another picture under a name taken
    4: ('background', P4),         # ... and again, in another case
    5: ('', P5),                   # no name
    6: ('Logo', P1),               # the same picture under the same name
}


def _captured():
    """What atemwire's download_media_pool_images returns for POOL."""
    return [{'slot': s, 'name': n or f'Slot{s}', 'png_bytes': p} for s, (n, p) in POOL.items()]


def test_each_slot_gets_a_file_of_its_own_picture():
    files = export._media_pool_files(_captured())
    assert files == {
        0: 'ATEM Media Pool/Logo.png',
        1: 'ATEM Media Pool/Logo copy.png',
        2: 'ATEM Media Pool/Background.png',
        3: 'ATEM Media Pool/Background_2.png',
        4: 'ATEM Media Pool/background_3.png',
        5: 'ATEM Media Pool/Slot5.png',
        6: 'ATEM Media Pool/Logo.png',
    }


def _profile_xml():
    stills = ''.join(
        f'<Still index="{s}" name="{n}" path="{f"ATEM Media Pool/{n}.png" if n else ""}"/>'
        for s, (n, _) in POOL.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<Profile majorVersion="1" minorVersion="5" '
            f'product="ATEM Test"><MediaPool><Stills>{stills}</Stills></MediaPool></Profile>\n')


def _save(monkeypatch):
    class _Atem:
        connected = True
        product_name = 'ATEM Test'

        class raw:
            mixerstate = {'mediaplayer-file-info': {}}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(export, 'ATEM', lambda ip: _Atem())
    monkeypatch.setattr(export.Profile, 'from_atem',
                        staticmethod(lambda atem, opts: Profile.from_xml(_profile_xml())))
    monkeypatch.setattr(export, 'download_media_pool_images', lambda atem, **kw: _captured())
    payload, content_type, _ = export.export_profile_zip('10.9.9.6', export.build_save_options({}))
    assert content_type == 'application/zip'
    return zipfile.ZipFile(io.BytesIO(payload))


def test_the_zip_holds_every_file_the_xml_names(monkeypatch):
    z = _save(monkeypatch)
    xml_name = next(n for n in z.namelist() if n.endswith('.xml'))
    stills = ET.fromstring(z.read(xml_name)).findall('./MediaPool/Stills/Still')
    assert len(stills) == len(POOL)
    for st in stills:
        slot = int(st.get('index'))
        assert z.read(st.get('path')) == POOL[slot][1]             # each path: that slot's picture
        assert st.get('name') == POOL[slot][0]                     # names untouched
    pngs = sorted(n for n in z.namelist() if n.endswith('.png'))
    assert pngs == sorted({'ATEM Media Pool/Logo.png', 'ATEM Media Pool/Logo copy.png',
                           'ATEM Media Pool/Background.png', 'ATEM Media Pool/Background_2.png',
                           'ATEM Media Pool/background_3.png', 'ATEM Media Pool/Slot5.png'})


def test_a_restore_gives_every_slot_its_picture_and_its_name(client, monkeypatch):
    z = _save(monkeypatch)
    xml_name = next(n for n in z.namelist() if n.endswith('.xml'))

    class _Atem:
        connected = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Result:
        applied, skipped, errors = [], [], []

        def summary(self):
            return 'ok'

    uploads = {}

    class _Done:
        def __init__(self, slot):
            self.slot, self.success, self.error = slot, True, None

    def fake_execute_upload(items, **kw):
        for _ip, slot, path in items:
            with open(path, 'rb') as f:
                uploads[slot] = (path.rsplit('/', 1)[-1], f.read())
        return [_Done(slot) for _ip, slot, _ in items]

    monkeypatch.setattr(views, 'ATEM', lambda ip: _Atem())
    monkeypatch.setattr(Profile, 'apply', lambda self, atem, options: _Result())
    monkeypatch.setattr(views, 'execute_upload', fake_execute_upload)

    form = {'ip': '10.9.9.6', 'sections': json.dumps({}),
            'profile': io.BytesIO(z.read(xml_name))}
    form['profile'].name = 'show.xml'
    for i, n in enumerate(n for n in z.namelist() if n.endswith('.png')):
        f = io.BytesIO(z.read(n))
        f.name = n.rsplit('/', 1)[-1]
        form[f'image_{i}'] = f
    r = client.post('/atem/profile/load/', form)
    assert r.status_code == 200, r.content
    images = r.json()['images']
    assert images['missing'] == [] and images['extra'] == [] and images['uploaded'] == len(POOL)
    for slot, (name, picture) in POOL.items():
        assert uploads[slot] == (f'{name or f"Slot{slot}"}.png', picture)   # the uploader names the slot from this


def test_a_slot_name_cannot_leave_the_restore_s_folder(tmp_path):
    path = views._slot_image_path(str(tmp_path), {'slot': 3, 'name': '../../etc/x'}, 'x.png')
    assert path.startswith(str(tmp_path / '3') + '/') and '/' not in path.rsplit('/3/', 1)[1]
    assert views._slot_image_path(str(tmp_path), {'slot': 4, 'name': ''}, 'Slot4.png').endswith('/4/Slot4.png')
