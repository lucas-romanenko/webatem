"""HyperDeck transport views reject a non-IP address.

state / status / transport pass the caller-supplied ``ip`` straight into
``with_deck`` (a 9993 TCP connect). A hostname or decimal-integer form is a
SSRF / port-scan primitive, so each view validates that the IP is a literal
before it connects. A valid IP still reaches ``with_deck`` (the guard filters
shape, not reachability). Standalone WebATEM has no login, so a bare client
reaches the views.
"""
from unittest import mock

import pytest

BAD_IPS = ["not-an-ip", "2852039166", "deck.example.com", "-oProxyCommand", ""]


@pytest.mark.django_db
@pytest.mark.parametrize("bad", BAD_IPS)
def test_state_bad_ip_400_no_connect(client, bad):
    with mock.patch("atem_control.hyperdeck.views.with_deck") as wd:
        r = client.get("/atem/hyperdeck/state/", {"ip": bad})
    assert r.status_code == 400
    wd.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("bad", BAD_IPS)
def test_status_bad_ip_400_no_connect(client, bad):
    with mock.patch("atem_control.hyperdeck.views.with_deck") as wd:
        r = client.get("/atem/hyperdeck/status/", {"ip": bad})
    assert r.status_code == 400
    wd.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("bad", BAD_IPS)
def test_transport_bad_ip_400_no_connect(client, bad):
    with mock.patch("atem_control.hyperdeck.views.with_deck") as wd:
        r = client.post("/atem/hyperdeck/transport/",
                        {"ip": bad, "action": "play"},
                        content_type="application/json")
    assert r.status_code == 400
    wd.assert_not_called()


@pytest.mark.django_db
def test_status_valid_ip_reaches_deck(client):
    with mock.patch("atem_control.hyperdeck.views.with_deck",
                    return_value={"status": "stopped"}) as wd:
        r = client.get("/atem/hyperdeck/status/", {"ip": "192.168.1.241"})
    assert r.status_code == 200
    wd.assert_called_once()
