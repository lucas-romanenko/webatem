# Security policy

## Threat model — read this before deploying

WebATEM is a **LAN control surface for broadcast hardware**, with the
same trust model as the physical panel it replaces:

- **There is no login, by design.** Anyone who can reach the app's port
  can switch program, change keyers, upload media, and drive connected
  HyperDecks. This is a deliberate parity with hardware panels and with
  the ATEM protocol itself — the switcher accepts commands from anyone
  on its network with or without this app in the picture.
- The intended deployment is a **trusted studio network** (ideally its
  own VLAN with the switchers). If a device can't be trusted with your
  program feed, it doesn't belong on that network segment.

## What the app does defend

- **Cross-origin browser attacks.** The control WebSocket only accepts
  browser connections from the app's own pages (same-origin, enforced
  independently of `ALLOWED_HOSTS` — see `config/websocket.py`), and all
  state-changing HTTP endpoints require a CSRF token. A malicious
  website open in an operator's browser cannot drive the switchers
  through it.
- **Hostile file content.** Switcher-profile XML parsing is hardened
  against entity-expansion attacks (including the UTF-16 detection
  bypass) and size-capped; uploaded images are re-encoded, never served
  back raw (there is no public `/media/` route).
- **Runtime surface.** The container runs as a non-root user, and the
  frontend loads no third-party code at runtime (all assets are
  self-hosted — no CDN supply chain).

## What it does not defend

- **No authentication or authorization** — see above.
- **No TLS** — terminate it at a reverse proxy.
- **No rate limiting.**

## Exposing it beyond the studio LAN

Put an authenticating TLS reverse proxy in front (basic auth or SSO),
and set:

- `CSRF_TRUSTED_ORIGINS=https://your.host`
- `ALLOWED_HOSTS=your.host` — pinning this also closes the DNS-rebinding
  variant of the cross-origin attack
- `WEBSOCKET_ALLOWED_ORIGINS=your.host` — only if the proxy rewrites the
  `Host` header on the way through

Never expose the app port directly to an untrusted network.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Security" tab →
"Report a vulnerability") rather than a public issue. Reports that
assume the threat model above are very welcome; "there is no login" is
the documented design, not a finding.
