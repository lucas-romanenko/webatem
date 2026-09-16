"""The seam: everything a host can supply goes through atem_control.hooks,
and the defaults are standalone WebATEM's behaviour."""
import asyncio
import json

import pytest
from django.http import HttpResponseForbidden
from django.test import override_settings

from atem_control import activity, hooks, sightings


class RecordingHooks(hooks.Hooks):
    """A host that refuses the socket, denies pages, names switchers and
    remembers every sighting and audit call."""
    def __init__(self):
        self.calls = []

    def check_access(self, request):
        return HttpResponseForbidden('no') if request.GET.get('deny') else None

    def can_use(self, user):
        # like a real host: this reads the database (it must run off the loop)
        from django.contrib.auth import get_user_model
        get_user_model().objects.count()
        return False

    def record_activity(self, **kw):
        self.calls.append(('activity', kw.get('action')))

    def record_connection(self, *, user, event_type, ip, data):
        self.calls.append(('connection', event_type, ip, data.get('disconnect_reason')))

    def recent_atems(self, request, limit=5):
        return [{'ip_address': '10.1.1.1', 'equipment_name': 'Host Room 1', 'last_connected': None}]

    def name_for_ip(self, ip):
        return 'Host Room 1' if ip == '10.1.1.1' else None

    def switchers(self):
        return [{'name': 'Host Room 1', 'ip': '10.1.1.1', 'location': 'Floor 2'}]

    def hyperdeck_names(self):
        return {'10.1.1.50': 'Deck A'}

    def discovery_enabled(self):
        return False

    def time_format_24h(self):
        return False

    def record_video_mode(self, ip, label):
        self.calls.append(('video_mode', ip, label))

    def record_deck_model(self, ip, model):
        self.calls.append(('deck_model', ip, model))

    def probe_device(self, ip):
        self.calls.append(('probe', ip))
        return {'deviceName': 'Room One', 'productName': 'ATEM Mini Pro', 'software': '9.0', 'hostname': 'atem'}

    def record_named(self, ip, name):
        self.calls.append(('named', ip, name))

    def validate_still(self, ip, file_path, file_name):
        self.calls.append(('validate', ip, file_name))
        return (False, 'wrong size for this switcher') if file_name.startswith('bad') else (True, None)

    def upload_still(self, ip, slot, file_path, job_dir, user=None):
        self.calls.append(('upload', ip, slot))
        return False, 'queued elsewhere'

    def template_context(self, request):
        return {'host_banner': 'HOSTED'}


@pytest.fixture
def host_hooks(settings):
    settings.WEBATEM_HOOKS = 'tests.unit.test_hooks.RecordingHooks'
    hooks.reset()
    yield hooks.get()
    hooks.reset()


def test_defaults_are_standalone(settings):
    settings.WEBATEM_HOOKS = None
    hooks.reset()
    h = hooks.get()
    assert type(h) is hooks.Hooks and hooks.get() is h            # one instance per process
    assert h.check_access(None) is None and h.can_use(None) is True
    assert h.switchers() == [] and h.hyperdeck_names() == {} and h.discovery_enabled() is True
    assert h.record_video_mode('1.2.3.4', '1080p25') is None and h.hyperdeck_binding_diff(None, '1.2.3.4') is None
    assert h.time_format_24h() is True and h.template_context(None) == {}
    hooks.reset()


def test_facades_forward_to_the_host(host_hooks):
    activity.record_activity(feature='f', device='d', action='cut', target='1.2.3.4', summary='CUT')
    asyncio.run(activity.arecord_activity(action='auto'))
    sightings.record_video_mode('1.2.3.4', '1080p25')
    sightings.record_deck_model('1.2.3.5', 'HyperDeck Studio HD Mini')
    assert host_hooks.calls == [('activity', 'cut'), ('activity', 'auto'),
                                ('video_mode', '1.2.3.4', '1080p25'), ('deck_model', '1.2.3.5', 'HyperDeck Studio HD Mini')]


@pytest.mark.django_db
def test_pages_take_what_the_host_supplies(client, host_hooks):
    html = client.get('/atem/').content.decode()
    assert 'Host Room 1' in html and '10.1.1.1' in html            # the host's recent list
    assert 'discoverySection' not in html and 'atem_discovery.js' not in html
    data = json.loads(html.split('id="atem-equipment-data"')[1].split('</script>')[0].split('>', 1)[1])
    assert data == [{'name': 'Host Room 1', 'ip': '10.1.1.1', 'location': 'Floor 2'}]
    control = client.get('/atem/control/').content.decode()
    assert '"10.1.1.50": "Deck A"' in control
    assert client.get('/atem/api/lookup-name/?ip=10.1.1.1').json() == {'found': True, 'name': 'Host Room 1'}
    assert client.get('/atem/api/lookup-name/?ip=10.9.9.9').json() == {'found': False, 'name': None}
    assert client.get('/atem/api/discovered/').json()['disabled'] is True
    assert client.get('/atem/api/scan/').json() == {'atems': [], 'disabled': True}


@pytest.mark.django_db
def test_the_host_can_refuse_every_endpoint(client, host_hooks):
    for path in ('/atem/', '/atem/control/', '/atem/api/status/', '/atem/api/lookup-name/', '/atem/device-info/',
                 '/atem/hyperdeck/state/', '/atem/profile/save_dialog_init/'):
        assert client.get(path + '?deny=1').status_code == 403, path
    assert client.post('/atem/media-pool-upload/?deny=1').status_code == 403


@pytest.mark.django_db(transaction=True)
def test_the_socket_gate_is_the_host_s(host_hooks):
    from atem_control.control.consumer import ATEMConsumer
    closed = []

    class Probe(ATEMConsumer):
        def __init__(self):
            self.scope = {'user': None}
        async def close(self, code=None):
            closed.append(code)
        async def accept(self, *a, **k):
            raise AssertionError('must not accept')
    asyncio.run(Probe().connect())
    assert closed == [None]


def test_connections_are_recorded_through_the_host(host_hooks):
    from atem_control.control.logging import ATEMConnectionLoggingMixin

    class Probe(ATEMConnectionLoggingMixin):
        scope = {'user': None}
        connected_ip = None
        connect_time = None
        current_atem_name = None
    p = Probe()
    asyncio.run(p._log_connect('10.1.1.1'))
    asyncio.run(p._log_disconnect(reason='user_requested'))
    assert [c for c in host_hooks.calls if c[0] == 'connection'] == [
        ('connection', 'connection', '10.1.1.1', None), ('connection', 'disconnection', '10.1.1.1', 'user_requested')]


@pytest.mark.django_db
def test_default_connection_log_and_recent_list(settings):
    settings.WEBATEM_HOOKS = None
    hooks.reset()
    from atem_control.models import ATEMControlLog
    hooks.get().record_connection(user=None, event_type='connection', ip='10.2.2.2', data={'ip_address': '10.2.2.2'})
    row = ATEMControlLog.objects.get()
    assert row.user is None and row._meta.app_label == 'webatem_atem' and row._meta.db_table == 'webatem_atem_atemcontrollog'
    assert hooks.get().recent_atems(None)[0]['ip_address'] == '10.2.2.2'
    hooks.reset()


@pytest.mark.django_db
def test_upload_hands_the_file_to_the_host(client, host_hooks, tmp_path, settings):
    from PIL import Image
    settings.DATA_DIR = tmp_path
    img = tmp_path / 'still.jpg'
    Image.new('RGB', (1920, 1080), (10, 20, 30)).save(img, 'JPEG')
    with open(img, 'rb') as f:
        r = client.post('/atem/media-pool-upload/', {'ip': '10.1.1.1', 'slot': 3, 'image': f})
    assert r.status_code == 400 and r.json()['error'] == 'queued elsewhere'
    assert ('validate', '10.1.1.1', 'still.jpg') in host_hooks.calls and ('upload', '10.1.1.1', 3) in host_hooks.calls
    bad = tmp_path / 'bad.jpg'
    Image.new('RGB', (640, 480)).save(bad, 'JPEG')
    with open(bad, 'rb') as f:
        r = client.post('/atem/media-pool-upload/', {'ip': '10.1.1.1', 'slot': 3, 'image': f})
    assert r.status_code == 400 and 'wrong size' in r.json()['error']


def test_profile_filenames_take_the_host_s_name_and_clock(host_hooks):
    from atem_control.profile import export
    assert export._resolve_atem_db_name('10.1.1.1') == 'Host Room 1' and export._resolve_atem_db_name('10.9.9.9') == ''
    # a 12-hour host: the timestamp carries the AM/PM marker the 24-hour format lacks
    assert export._format_save_timestamp().endswith(('AM', 'PM'))


def test_connection_log_carries_the_switcher_name(host_hooks):
    from atem_control.control.logging import ATEMConnectionLoggingMixin

    class Probe(ATEMConnectionLoggingMixin):
        scope = {'user': None}
        connected_ip = None
        connect_time = None
        current_atem_name = 'Host Room 1'
    asyncio.run(Probe()._log_connect('10.1.1.1'))
    assert ('connection', 'connection', '10.1.1.1', None) in host_hooks.calls


def test_default_still_check_refuses_a_decompression_bomb(tmp_path):
    """"Any image at any size" is about the picture, not about what the process
    will decode. Pillow's ceiling stays at its default (it is NOT disabled), and
    it fires on the header's declared size inside Image.open, before a pixel is
    decoded. Named explicitly because DecompressionBombError is neither an
    OSError nor a ValueError, so an unnamed one escapes as an unhandled
    exception instead of a refusal. One ASGI worker by design: a 3 GB decode
    mid-show is not a thing to find out about in production."""
    from PIL import Image
    from atem_control.hooks import Hooks

    # Asserted deliberately: the obvious future "fix" when a bomb error
    # escapes somewhere is to set MAX_IMAGE_PIXELS = None and make the
    # symptom go away, which removes the only thing standing between a
    # crafted header and a multi-gigabyte decode on the one ASGI worker.
    assert Image.MAX_IMAGE_PIXELS is not None, 'the bomb ceiling must not be disabled'

    bomb = tmp_path / 'bomb.png'
    Image.MAX_IMAGE_PIXELS = None                      # only to write the file
    try:
        Image.new('L', (30000, 30000)).save(bomb, 'PNG', compress_level=9)
    finally:
        Image.MAX_IMAGE_PIXELS = 89478485              # the shipped default

    assert bomb.stat().st_size < 2_000_000, 'under 2 MB on disk, ~3 GB decoded'

    ok, err = Hooks().validate_still('10.1.1.1', str(bomb), 'bomb.png')
    assert not ok
    assert 'too large to decode' in err


def test_default_still_check_takes_any_image(tmp_path, settings):
    """Drop what you like on a slot, as in ATEM Software Control: the upload
    path fits it to the switcher's own frame, so the check is only 'is this an
    image'. A host that wants a stricter rule overrides validate_still."""
    from PIL import Image
    from atem_control.hooks import Hooks
    h = Hooks()

    for name, size in [('uhd.jpg', (3840, 2160)),      # bigger than any frame
                       ('small.jpg', (1280, 720)),     # smaller, once refused
                       ('square.png', (500, 500)),     # not 16:9, once refused
                       ('tall.png', (400, 1200))]:     # portrait
        f = tmp_path / name
        Image.new('RGB', size).save(f)
        assert h.validate_still('10.1.1.1', str(f), name) == (True, None), name

    # Still refuses what is not an image at all.
    junk = tmp_path / 'notapicture.jpg'; junk.write_bytes(b'this is not a jpeg')
    ok, err = h.validate_still('10.1.1.1', str(junk), 'notapicture.jpg')
    assert not ok and 'not a readable image' in err


@pytest.mark.django_db
def test_switcher_name_section_goes_through_the_host(client, host_hooks, monkeypatch):
    d = client.get('/atem/device-info/?ip=10.1.1.1').json()
    assert d['supported'] and d['deviceName'] == 'Room One' and d['equipmentName'] == 'Host Room 1'
    assert ('probe', '10.1.1.1') in host_hooks.calls
    monkeypatch.setattr('atem_control.control.views.set_device_name', lambda ip, name: (True, None))
    r = client.post('/atem/device-name/', json.dumps({'ip': '10.1.1.1', 'name': 'Studio B'}), content_type='application/json')
    assert r.json() == {'success': True, 'deviceName': 'Studio B'}
    assert ('named', '10.1.1.1', 'Studio B') in host_hooks.calls and ('activity', 'set_device_name') in host_hooks.calls


def test_no_idle_disconnect_by_default():
    """A panel does not log itself out: the default hook says never, and a
    host that wants the old five minutes returns a number."""
    from atem_control import hooks as H
    from atem_control.control.consumer import ATEMConsumer
    assert H.Hooks().idle_disconnect_seconds() is None
    assert not hasattr(ATEMConsumer, 'INACTIVITY_TIMEOUT')       # no constant to fall back on

    class Impatient(H.Hooks):
        def idle_disconnect_seconds(self):
            return 42
    assert Impatient().idle_disconnect_seconds() == 42
