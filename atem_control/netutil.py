"""The app-wide caller-supplied-IP validation standard (audit batch 2).

Any IP that arrives from a request — form field, JSON body, WebSocket
message, URL — and then reaches a ``socket.connect`` or a subprocess argv
MUST pass through here first. ``ipaddress.ip_address`` rejects the three
things a naive ``^[\\d.]+$`` regex lets through, each of which turns a
permission-gated internal tool into an SSRF / port-scan / command-injection
primitive:

  * **decimal-integer form** — ``"2852039166"`` matches ``^[\\d.]+$`` and
    ``socket.connect`` expands it to ``169.254.169.254`` (link-local /
    cloud-metadata range);
  * **hostnames** — a bare name triggers a DNS lookup to an attacker-chosen
    host;
  * **``-``-prefixed / whitespace-padded strings** — argv option injection
    when the value lands in a ``ping``/subprocess command line.

This is the single standard; ``videohub_control`` and ``av_equipment.ping``
already validated this way, and every other caller was unified onto it.
Both shapes are strict and hostname-free.
"""
import ipaddress


def clean_ip(raw) -> str:
    """Return the normalized IPv4/IPv6 string, or ``''`` if ``raw`` is not a
    literal IP address (no hostnames, no integer form, no padding)."""
    try:
        return str(ipaddress.ip_address(str(raw or "").strip()))
    except ValueError:
        return ""


def is_valid_ip(raw) -> bool:
    """True iff ``raw`` is a literal IPv4/IPv6 address (no hostnames)."""
    return clean_ip(raw) != ""
