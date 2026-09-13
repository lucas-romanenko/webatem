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

    def record_video_mode(self, ip, label):
        self.calls.append(('video_mode', ip, label))

    def record_deck_model(self, ip, model):
        self.calls.append(('deck_model', ip, model))

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
    assert ('upload', '10.1.1.1', 3) in host_hooks.calls
