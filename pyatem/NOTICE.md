# pyatem — fork notice

This directory contains a substantially modified fork of **pyatem**, the
Blackmagic ATEM protocol library from the OpenAtem project:

- Upstream: https://git.sr.ht/~martijnbraam/pyatem
- Copyright 2021–2022 Martijn Braam and the OpenAtem contributors
- License: LGPL-3.0-only (see `LICENSE` in this directory; the LGPL
  incorporates the GPL, included as `COPYING.GPL`)

Modified 2025–2026 by Lucas Romanenko for WebATEM. Modifications in this
fork include (non-exhaustive): a declarative wire-format DSL (`messages/`),
a hardened UDP transport (in-order delivery, contiguous-ACK gap detection,
go-back-N retransmit serving, clean session close), a ref-counted
connection pool, native interleaved bulk transfers, macro bytecode
transfer (`macrotransfer/`), XML switcher-state save/restore (`profile/`),
and a full-state assembler (`_state.py`).

Every file in this directory carries an `SPDX-License-Identifier:
LGPL-3.0-only` header; files retained from upstream additionally keep the
upstream copyright line plus a dated modification notice (GPLv3 §5(a)).
Files without an upstream copyright line were written for this fork and
are licensed under the same LGPL-3.0-only terms.

This fork remains licensed under **LGPL-3.0-only**. The surrounding
application (everything outside this directory) is separately licensed —
see the repository root `LICENSE`.
