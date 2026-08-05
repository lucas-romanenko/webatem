"""Smoke tests proving the pytest-django harness works end-to-end.

Exercises the whole request stack: settings load, the auto-created test DB +
migrations, URL routing, middleware, and real template renders. There is
deliberately no login and no switcher database in this app — the Connect
page takes a bare IP, and Recent Connections come from the connection log.
"""
import pytest


@pytest.mark.django_db
def test_connect_page_renders(client):
    resp = client.get("/atem/")

    assert resp.status_code == 200
    assert b"Connect to ATEM" in resp.content


@pytest.mark.django_db
def test_recent_connections_render_from_log(client):
    from atem_control.models import ATEMControlLog

    ATEMControlLog.objects.create(type='connection',
                                  data={'ip_address': '10.20.30.40'})

    resp = client.get("/atem/")

    assert resp.status_code == 200
    assert b"10.20.30.40" in resp.content


@pytest.mark.django_db
def test_control_page_renders(client):
    resp = client.get("/atem/control/?ip=192.168.1.50")

    assert resp.status_code == 200
