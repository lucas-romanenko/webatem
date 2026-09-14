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


@pytest.mark.django_db
def test_the_pages_that_post_set_the_csrf_cookie():
    """The control page's POSTs (switcher name, media pool, profile) read the
    csrftoken cookie. 0.5.0 removed the last template that set it and every
    one of them became a 403 the page reported as a failure — the Switcher
    Name section said "Failed to set the name" while the switcher was fine.
    """
    from django.test import Client
    c = Client(enforce_csrf_checks=True)
    assert 'csrftoken' in c.get('/atem/').cookies
    r = c.get('/atem/control/?ip=192.168.1.240')
    assert r.status_code == 200 and 'csrftoken' in r.cookies
    token = r.cookies['csrftoken'].value
    # a POST with that token is no longer rejected (it reaches the view,
    # which answers about the switcher rather than about CSRF)
    posted = c.post('/atem/device-name/', '{"ip": "192.0.2.1", "name": "x"}',
                    content_type='application/json', HTTP_X_CSRFTOKEN=token)
    assert posted.status_code != 403
