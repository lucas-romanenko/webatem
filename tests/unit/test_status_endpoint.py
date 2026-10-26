"""/atem/api/status/ reports M/E 1's buses, transition and FTB.

bmdwire's build_full_state keeps them per M/E under ``mes[]`` and has no
top-level ``program``/``preview``/``transition``/``ftb``; the endpoint read
those and answered program 0 for every switcher (#40). The states here are
bmdwire's own snapshots, so the test follows the shape it really builds.
Standalone WebATEM has no login, so a bare client reaches the view.
"""
import copy
import json
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).parent
IP = '10.0.0.9'


def _snapshot(name):
    return json.loads((HERE / name).read_text())


def _status(client, state):
    with mock.patch('atem_control.control.views.ATEMInstanceManager') as pool, \
            mock.patch('atem_control.control.views.build_full_state', return_value=state):
        pool.list_instances.return_value = {IP: {'connected': True, 'ref_count': 1, 'state': 'active'}}
        pool.get_instance.return_value = {'connection': object()}
        r = client.get('/atem/api/status/', {'ip': IP})
    assert r.status_code == 200
    return r.json()


@pytest.mark.django_db
def test_status_reports_me1(client):
    state = _snapshot('build_full_state_snapshot.json')
    assert 'program' not in state and state['mes'][0]['program'] == 2   # the shape the bug missed
    got = _status(client, state)
    assert (got['program'], got['preview']) == (2, 1)
    assert got['transition_active'] is False and got['ftb_active'] is False and got['ftb_disabled'] is False


@pytest.mark.django_db
def test_status_reads_me1_not_another(client):
    # M/E 2 is mid-transition on 4/5 in this snapshot; M/E 1 is not.
    state = _snapshot('build_full_state_multi_me_snapshot.json')
    got = _status(client, state)
    assert (got['program'], got['preview'], got['transition_active']) == (2, 1, False)


@pytest.mark.django_db
def test_status_reports_me1_transition_and_ftb(client):
    state = copy.deepcopy(_snapshot('build_full_state_snapshot.json'))
    state['mes'][0]['transition']['in_transition'] = True
    state['mes'][0]['ftb'].update(active=True, disabled=True)
    got = _status(client, state)
    assert got['transition_active'] is True and got['ftb_active'] is True and got['ftb_disabled'] is True


@pytest.mark.django_db
def test_status_without_mes_keeps_its_shape(client):
    got = _status(client, {'is_connected': True})
    assert (got['program'], got['preview'], got['transition_active'], got['ftb_active'], got['ftb_disabled']) == \
        (0, 0, False, False, False)
