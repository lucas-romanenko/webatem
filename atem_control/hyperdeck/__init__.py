"""HyperDeck transport control for the ATEM control page.

Drives ATEM-bound HyperDecks directly over their TCP/9993 control protocol
(via the vendored ``pyhyperdeck`` client) so operators can browse clips and
play / pause / stop / loop from the control page. HTTP endpoints
(``hyperdeck/state``, ``hyperdeck/transport``) backed by a per-IP cached
connection — see ``connection.py`` and ``views.py``.
"""
