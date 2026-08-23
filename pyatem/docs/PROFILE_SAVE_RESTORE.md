# Profile Save / Restore — Feature Documentation

This document describes the **end-to-end save/restore feature** built on
top of `pyatem.profile` and `pyatem.macrotransfer`: what it does, how an
operator uses it, how the pipeline is wired together, what API surface
it exposes, and what's known not to work.

For the underlying XML and bytecode wire-format details, see:

- [`PROFILE_FORMAT.md`](PROFILE_FORMAT.md) — the `<Profile>` XML schema as
  emitted by ATEM Software Control's *Save Switcher State* command.
- [`MACRO_FORMAT.md`](MACRO_FORMAT.md) — the macro recording / playback /
  upload wire protocol.

---

## What the feature does

Save and restore a complete ATEM switcher configuration to a single file
that is **byte-identical compatible with Blackmagic ATEM Software
Control's "Save Switcher State"** command. A profile saved by AV Server
opens cleanly in Software Control; a profile saved by Software Control
loads cleanly into AV Server.

This is — to our knowledge — the first publicly-implemented support for
the `<Profile>` format outside of Blackmagic's own tools.

A profile bundles, per ATEM:

| Section | What's covered | Notes |
|---|---|---|
| Mix-effect block | Program, Preview, Next-Transition mask, Transition Style + per-style params (mix/dip/wipe/stinger/DVE), Fade-to-Black, USK 1–4 | Per-element granularity in the dialog |
| Downstream Keys | DSK 1–2 sources, mask, rate, on-air state | |
| Color Generators | CG 1–2 hue/saturation/luma | |
| Auxiliaries | Aux 1–4 routing | |
| Settings | FtB enabled, multiview layout, button mapping | |
| Video Mode | Resolution + frame rate | Destructive — outputs drop ~3 s on apply |
| Inputs | Input long/short labels (rename) | |
| Fairlight Audio Mixer | Master fader, per-strip fader/gain/mix-type/EQ enable + bands, master EQ bands | Compressor / limiter / expander not yet writable |
| Media Players | MP1 / MP2 slot bindings | |
| Media Pool | Per-slot still images (PNG) + metadata | Optional — operator chooses at save time |
| Macros | Up to 100 macro slots; bytecode form | First public support outside Blackmagic |
| HyperDecks | Deck IP + switcher input per slot | Saved **and** restored (CXMS, 2026-06-09) |
| Camera Control, Talkback, Multiview routing, Counters | Saved to XML, **not** restored | No setter ops available yet |

---

## Operator workflow

### Save

1. Open the ATEM control page for the target switcher.
2. Click **Save State** (next to *Media Upload*).
3. The save dialog opens with checkboxes grouped to mirror Software
   Control's *Save Switcher State* dialog:
   - **Switcher** — an *M/E 1* tab box containing 9 cells (Program,
     Preview, Next Transition, Transition Style, Fade To Black, USK
     1–4) + global section row (Downstream Keys, Color Generators,
     Camera Control, Audio Mixer).
   - **Media** — Media Players, Media Pool metadata, Media Pool images.
   - **Input / Output** — Auxiliaries, Counters.
   - **Others** — Settings, Video Mode, HyperDecks, Macros.
4. Sections backed by features the switcher topology doesn't have
   (SuperSource on a 1 M/E box, additional M/Es, etc.) and sections
   backed by apply-side gaps (HyperDeck binding, Camera Control apply,
   Stream/Record, Audio Mapping, Counter, Remote Source) are rendered
   greyed-out with a hover tooltip explaining why.
5. *Select All / Select None* buttons toggle in bulk.
6. Click **Save**. The browser shows *"Capturing media pool images…"* if
   images are checked (5–10 s per used slot — synchronous). The download
   triggers on completion:
   - **ZIP** if any images were captured: `<basename>.xml` at root +
     `ATEM Media Pool/<filename>.png` entries — exactly matching
     Software Control's on-disk layout.
   - **Bare XML** otherwise.
7. The download filename uses the equipment's database name, falling
   back to the ATEM model name, with a timestamp appended.

### Load

1. Click **Load State**. A file picker opens.
2. Select an XML file (or a folder containing one — see *Working with
   ZIP archives* below).
3. The load dialog opens; checkboxes are pre-populated against the
   profile's contents — only sections present in the loaded XML appear
   as enabled, with the rest hidden. Defaults are conservative:
   **Program/Preview pre-unchecked** (so Apply doesn't unexpectedly cut
   to air); other sections pre-checked.
4. The dialog also lists *referenced images* — the filenames that XML
   `<Still>` entries point at.
5. If *Media Pool images* is checked, the dialog prompts for the image
   files (operator selects them from disk). The matching is
   case-insensitive on filename only.
6. Click **Load**. The XML is applied first, then the images are
   uploaded to the chosen slots.
7. The result modal reports per-section status:
   - `applied: 9, skipped: 5, errors: 0` summary line.
   - Per-section drill-down. Skipped sections include the reason
     (option default, no setter, gap).
   - Image upload report: `uploaded: N`, `missing: [filenames in XML
     but not provided]`, `extra: [filenames provided but not in XML]`,
     `upload_errors: [...]`.

### Working with ZIP archives

Saves with media-pool images selected produce a single ZIP. To load:

- Some browsers (modern Chromium with File System Access API support)
  used to let the operator pick a folder directly. **That path was
  removed** because it errored on common locations (Downloads, home
  folder) — Chrome refuses `showDirectoryPicker` access there as a
  security feature. Today's load path is the universal fallback that
  works in every browser.
- Operator extracts the ZIP first. The XML and the `ATEM Media Pool/`
  subdirectory will be alongside each other. Pick the XML file to
  start; pick the image files when prompted.

### Cross-studio cloning

The intended use case is cloning a switcher's setup from one studio to
another. To do this safely:

1. At the source studio, save with **all** sections checked (including
   Media Pool images).
2. Transfer the ZIP to the destination studio.
3. At the destination, load with the conservative defaults
   (Program/Preview unchecked). After the apply succeeds, the operator
   can manually set Program/Preview to a known-good source.

Video Mode is on by default and **will** change the destination
switcher's resolution if it differs from the source. Outputs drop for
~3 s while the ATEM resyncs. If both studios already run the same
resolution, this is a no-op.

---

## Pipeline architecture

```
       SAVE                                        RESTORE
       ──────                                      ─────────

  Browser: "Save State"                     Browser: "Load State"
       │                                         │
       │ GET /atem/profile/save_dialog_init/    │ POST /atem/profile/load_xml/  (XML file)
       │   → describe_save_sections(atem)       │   → Profile.from_xml() → describe_load_sections()
       │   → checkboxes per section             │   → checkboxes per section actually present
       │                                         │
       │ POST /atem/profile/save/  {sections}   │ POST /atem/profile/load/  (XML + image_N + sections)
       ▼                                         ▼
  Profile.from_atem(atem, SaveOptions)      Profile.apply(atem, ApplyOptions)
       │                                         │
       │  _wait_for_state_settled()              │  _apply_video_mode → _apply_inputs →
       │  _build_mix_effect_blocks               │  _apply_settings_flags → _apply_color_generators →
       │  _build_downstream_keys                 │  _apply_aux → _apply_transition →
       │  _build_color_generators                │  _apply_upstream_keyers (per-key gate) →
       │  _build_auxiliaries                     │  _apply_downstream_keys → _apply_fade_to_black →
       │  _build_settings + _build_video_mode    │  _apply_media_players → _apply_fairlight →
       │  _build_fairlight + _build_eq_bands     │  _apply_macros (bytecode upload) →
       │  _build_media_players                   │  _apply_program_preview (LAST — cuts to air)
       │  _build_media_pool_stills               │
       │  _build_macro_pool                      │  Returns ApplyResult{applied, skipped, errors}
       │   └─ macrotransfer                      │     plus image-upload report
       │       .download_macro_bytecode          │
       │       (FTSU/FTDa file-transfer)         │  Macros via:
       │       .decode_macro_bytecode            │   macrotransfer.encode_macro_bytecode → upload
       │       (146 op codes → <Op> elements)    │       (FTSD mode=0x0300)
       ▼                                         │
  Profile.to_xml()  (Software Control format)    ▼
       │                                         │
  download_media_pool_images(atem)          execute_upload(images) if checkbox set
   (per-slot raw_download_slot → PNG)        (matched by filename, case-insensitive)
       │
       ▼
  ZIP{ basename.xml, ATEM Media Pool/*.png } — or bare XML
```

### The key insight

The XML is created **client-side** by Software Control. The switcher
itself has no concept of a profile: it just emits state-dump packets.
Software Control receives those packets, serializes them to XML, and on
restore walks the XML and replays the corresponding wire commands. The
state going out and coming in is the same wire-protocol vocabulary
either way.

`pyatem.profile` follows the same model: `from_atem` reads
`pyatem`'s mixerstate (the parsed result of those same packets) and
serializes; `apply` walks the XML and dispatches to `pyatem.operations`
(the same vocabulary the WebSocket dispatch table uses for live
operator commands). This is what makes byte-identical round-trip
achievable without firmware cooperation.

### Macro path (separate)

Macros are an exception. The macro store on the switcher holds binary
opcode bytecode, not raw wire commands. The save side **downloads** the
bytecode via the file-transfer protocol (`FTSU` → `FTDa` chunks →
`FTUA` → `FTDC`, store id `0xFFFF`) and decodes it to XML `<Op>`
children. The restore side **uploads** newly-encoded bytecode via the
file-transfer protocol (`FTSD` mode `0x0300` — uncompressed +
pre-erase, the only mode the macro store accepts → `FTDa` chunks →
`FTFD`).

Both paths are bidirectional and round-trip byte-identical. The 146
known op codes cover everything Software Control emits for the
operator-accessible feature set on a 1 M/E Constellation HD.

For details, see [`MACRO_FORMAT.md`](MACRO_FORMAT.md).

### Why macros are NOT recorded on apply

The original implementation used the recording path (`MSRc` →
replay each `<Op>` as a wire command → `MAct` stop). The switcher
records whatever lands on the wire while in record mode. That works
but it has a major side effect: **every macro op drives live state
through the switcher as it's recorded.** Restoring a profile would
flicker program/preview, toggle keyers on/off, run transitions, etc.

The bytecode upload path lands the macro directly in its slot with
zero side effects. That is what `Profile.apply` uses today; the
recording-based ops (`macro_start_recording`, etc.) remain in
`pyatem.operations` for callers who explicitly need them.

---

## Public API surface

```python
from pyatem import Profile, ApplyOptions, SaveOptions

# Save side
profile = Profile.from_atem(atem)              # everything-on default
profile = Profile.from_atem(atem, SaveOptions(media_pool_images=False))
xml_str = profile.to_xml()
profile.to_file('/tmp/state.xml')

# Round-trip
Profile.from_xml(xml_str).to_xml()             # diff-equivalent

# Load + apply
profile = Profile.from_file('/tmp/state.xml')
result = profile.apply(atem)                   # conservative defaults
result = profile.apply(atem, ApplyOptions(restore_program=True,
                                          restore_preview=True))
print(result.summary())   # "applied: 9, skipped: 5, errors: 0"
print(result.applied)     # ['video_mode', 'settings_flags', ...]
print(result.skipped)     # ['program_preview: gated off (...)', ...]
print(result.errors)      # ['fairlight: ValueError: ...']

# Section descriptors (for building UI)
# Note: these live in the AV Server application rather than pyatem —
# the descriptor shape is UI scaffolding for the section-selection
# modal, not part of the save/restore feature.
from atem_control.profile.dialog import (
    describe_save_sections, describe_load_sections,
)
descriptor = describe_save_sections(atem)
descriptor = describe_load_sections(profile)
# Returns {'sections': [...], 'me_tabs': [1], 'referenced_images': [...]}

# Media pool images (save side)
from pyatem.profile import download_media_pool_images
images = download_media_pool_images(atem)      # [{'name': '...', 'png_bytes': b'...'}, ...]
```

### `SaveOptions` (everything defaults to `True`)

| Field | Gates emission of |
|---|---|
| `program`, `preview` | M/E `<Program>` / `<Preview>` |
| `next_transition`, `transition_style`, `fade_to_black` | M/E sub-elements |
| `usk: List[bool]` | `<Key index="i">` per i |
| `downstream_keys`, `color_generators`, `fairlight`, `camera_control` | Switcher-global elements |
| `media_pool_metadata`, `media_pool_images`, `media_players` | Media |
| `auxiliaries`, `counters` | I/O |
| `settings`, `video_mode`, `hyperdecks`, `macros` | Others |

### `ApplyOptions` (selective defaults)

| Field | Default | Notes |
|---|---|---|
| `restore_program`, `restore_preview` | `False` | Cuts-to-air gate |
| `restore_next_transition`, `restore_transition_style`, `restore_fade_to_black` | `True` | |
| `restore_usk: List[bool]` | `[True]*4` | |
| `restore_downstream_keys`, `restore_color_generators`, `restore_audio` | `True` | |
| `restore_video_mode` | `True` | **Destructive** — ~3 s output drop on resync |
| `restore_inputs`, `restore_macros`, `restore_aux`, `restore_media_players`, `restore_settings_flags` | `True` | |
| `restore_media_pool_images` | `True` | Requires images supplied via load endpoint |
| `restore_hyperdecks` | `True` | Deck IP + switcher input via CXMS |
| `restore_multiview`, `restore_camera_control`, `restore_talkback` | `False` | No setter ops available |

### `ApplyResult`

```python
@dataclass
class ApplyResult:
    applied: List[str]     # section names that ran
    skipped: List[str]     # "section: reason" strings
    errors: List[str]      # "section: ExceptionType: message" strings

    def summary(self) -> str: ...
```

`apply()` is best-effort: a failure in one section appears in `errors`
but does not abort the rest of the walk.

### Apply order

Order matters because some sections set up state others depend on:

1. Video mode (changes resolution; ~3 s output drop)
2. Inputs / labels
3. Settings flags (FtB enable, etc.)
4. Color generators
5. Auxiliaries
6. Transition style + per-style params + next-transition mask
7. USK type → per-type params (chroma needs type=Chroma set first)
8. DSK
9. Fade-to-black rate
10. Media player slot assignments
11. Fairlight (master + strips + EQ)
12. Macros (bytecode upload)
13. **Program / Preview** (last — they're the cuts-to-air gate)

---

## HTTP endpoints (Django)

All endpoints live under `/atem/profile/` and require the
`ATEMConsumer`-style `?ip=<ip>` parameter (or POST body equivalent).

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/profile/save_dialog_init/` | GET | `?ip=...` | `{sections, me_tabs, atem_name, ip}` |
| `/profile/save/` | GET | `?ip=...` | XML download (no images) |
| `/profile/save/` | POST | `{ip, sections}` | ZIP or XML download depending on selection |
| `/profile/load_xml/` | POST | multipart `profile=<xml>` | `{sections, me_tabs, referenced_images, xml, size}` |
| `/profile/load/` | POST | multipart `ip, profile, sections, image_<n>` | `{applied, skipped, errors, images}` |

Error shape: HTTP 400 for malformed request, 503 for connection
failures, 500 for internal errors. JSON body always includes a
human-readable `error` field.

---

## Frontend

Lives in `atem_control/static/js/atem_profile.js` (~445
lines). Public surface on `window.AtemProfile`:

| Function | Purpose |
|---|---|
| `openSaveDialog(ip, alpine)` | Fetch descriptor, populate Alpine state |
| `openLoadDialog(ip, alpine)` | File-pick XML, post to load_xml, render descriptor |
| `runDialog(ip, alpine)` | Dispatch to runSave or runLoad |
| `selectAll(selections, descriptor, value)` | Bulk checkbox toggle |
| `saveProfileFiles(blob, filename)` | Trigger the browser download |
| `loadProfileFiles(opts)` | Open file picker(s) |
| Layout helpers | `sectionsInGroup`, `sectionsInMeTab`, `meTabs` for Alpine `x-for` iteration |

The dialog markup lives in `atem_control/templates/control.html`
(lines 1814–1970). It uses Alpine.js directives — no heavy framework.

The earlier session-2 File System Access API path (direct folder
read/write) was removed: Chrome refuses `showDirectoryPicker` access
to Downloads / home / common locations as a security feature, which
made the "modern" path actively worse than the universal fallback.
Today there is one path: standard `<input type="file">` for picks,
`<a download>` for the response.

---

## Tests

| File | What it covers | Count |
|---|---|---|
| `tests/unit/test_profile_roundtrip.py` | Format compliance + reference XML byte-identity round-trip + apply-defaults sanity | 13 |
| `tests/unit/test_profile_granular.py` | Each of the 9 M/E grid flags in isolation, save-side and apply-side. Mock `_RecordingConn` captures emitted commands | 20 |
| `tests/unit/test_apply_macros.py` | Macro apply policy: clean uploads, partial uploads (encoder gaps), all-dropped fallback to metadata-only, transfer errors | 13 |
| `tests/unit/test_macro_decoder.py` | Per-helper decoder tests + module-load symmetry guard | 17 |
| `tests/unit/test_macro_encoder.py` | Per-op encoder tests including byte-identical round-trip on live ATEM captures (e.g. the "WIDE CAM" macro at 712 bytes / 52 ops) | 33 |

Reference XML: `everything_2026-04-27_18-16-51.xml` (Constellation HD,
v2.1). The round-trip on this file is **byte-identical** at 71,987
characters and is the format-compliance pin.

Live-ATEM smoke / verification scripts live in `av_server/tools/`:

- `profile_macro_verify.py` — runs `Profile.from_atem`, reports
  per-op-id histogram + any `Unknown_0x` artifacts.
- `profile_macro_roundtrip_smoke.py` — end-to-end record → save →
  download → assert no unknowns.
- `macro_opcode_discovery.py` — records single-field no-op commands
  into a free macro slot to discover op codes for unmapped XML ids.

Run with `pytest tests/`. No live ATEM required.

---

## What doesn't work yet

These show in the dialog as greyed-out (with a tooltip on hover) or
appear in `ApplyResult.skipped` with the reason `"… ops not implemented
(gap)"`. They're documented limitations, not bugs:

- **HyperDeck binding** — deck IP + switcher input saved and restored (CXMS,
  2026-06-09). auto-roll / frame-delay not yet wire-mapped.
- **Camera Control** — saves the parameters, no apply-side setter ops.
- **Multiview routing** — saves the layout, no apply-side setter ops.
- **Talkback** — no save or apply-side ops.
- **Stream / Record** — no save or apply.
- **Audio Mapping** — no save or apply.
- **Counter / display-clock** — no save or apply.
- **Remote Source** — no save or apply.
- **Fairlight Compressor / Limiter / Expander** — receive packets are
  parsed (visible in mixerstate); send-side wire commands haven't been
  reverse-engineered. Master fader, per-strip fader/gain/mix/EQ, and
  master EQ all work.
- **SuperSource** — the format supports it; AV Server doesn't render a
  dialog cell for it because production switchers don't have it. Would
  need a topology-driven dialog.
- **Multi-M/E** — same. Today the M/E box is hardcoded for "M/E 1".

### Cross-model assumptions

Section descriptors and the dialog layout are fixed for a 1 M/E
Constellation HD. Other ATEM models likely emit a strict superset of
the XML. The XML parser is permissive (unknown attributes preserved
verbatim, unknown elements ignored), so loading a save from a bigger
ATEM should not crash — but apply will silently no-op anything outside
the supported set.

### Resolution mismatch (latent)

`content_change/views/upload.py` always resizes to 1920×1080. If the
target ATEM runs anything other than 1080p, profile-driven media-pool
restore will fail validation. All production switchers run 1080p so
this isn't fired today.

---

## How to extend

### Adding a new section to save

1. Decide if it has a setter operation in `pyatem/operations.py`. If
   not, add it. The wire-format work goes in
   `pyatem/messages/<feature>.py` — declare a `Send` (or `Recv`) class
   using the DSL field types (`u8`, `u16`, etc.).
2. Add a `_build_<section>(root, mx, opts)` function in
   `pyatem/profile.py`. Read from the `mx` (mixerstate) dict; emit
   sub-elements on `root`.
3. Add a `<flag>` field on `SaveOptions` defaulting `True`.
4. Wire it into `Profile.from_atem` after the existing build calls.
5. Add a section to `describe_save_sections` in
   `atem_control/profile/dialog.py` so the AV Server save
   modal renders a checkbox.
6. Add a parametrized round-trip test in
   `tests/unit/test_profile_granular.py`.

### Adding a new section to apply

1. Make sure operation(s) exist in `pyatem/operations.py`.
2. Add `_apply_<section>(conn, root, result)` in `pyatem/profile.py`.
   Read XML attributes; call operations. Wrap in try/except and use
   `result.note_*`.
3. Add a `restore_<section>` field on `ApplyOptions` with a default
   that matches the operational risk (`True` if the op is safe and the
   intended use case wants it).
4. Wire into `Profile.apply` in the documented order.
5. Add a section to `describe_load_sections` in
   `atem_control/profile/dialog.py`.
6. Add tests in `tests/unit/test_profile_granular.py` and (if needed)
   round-trip tests in `tests/unit/test_profile_roundtrip.py`.

### Adding a macro op

The 146-entry `_KNOWN_OPS` table in `pyatem/macrotransfer.py` is
3-tuple `(xml_id, decoder, encoder)`. The module-load symmetry guard
(`tests/unit/test_macro_decoder.py::test_known_ops_table_entries_are_3_tuples`)
ensures no half-implementations.

The discovery workflow (proven across 99 ops added in one pass on
2026-04-30):

1. Have an operator record a comprehensive macro on a real ATEM with
   Software Control, exporting the XML.
2. Use `av_server/tools/macro_opcode_discovery.py` to record the same
   single-field operation in isolation into a test slot, download the
   bytecode, and inspect.
3. Or, with a comprehensive XML in hand, walk the XML's `<Op>` children
   in parallel with the bytecode bytes — every position-aligned pair
   gives `op_code → xml_id` and the param bytes derive the encoding.
4. Add a decoder + encoder pair (each is usually 4–10 lines using one
   of the existing layout helpers — `me_marker_u16`, `strip_prefix`,
   `gen_marker_fp`, etc.).
5. Add an entry to `_KNOWN_OPS`.
6. Add a parametrized round-trip test in
   `tests/unit/test_macro_encoder.py`.

### Closing a Fairlight gap (compressor / limiter / expander)

The receive side parses the response packets (visible in mixerstate
under `fairlight-audio-input` strip-id keys). Closing the apply gap
requires reverse-engineering the **send** packets:

1. Capture a Software Control session adjusting the relevant parameter
   in Wireshark.
2. Identify the matching command (likely `CFCP` for compressor, `CFLP`
   for limiter, `CFXP` for expander — by analogy with `CFSP` strip /
   `CFMP` master / `CEBP` EQ band).
3. Add the `Send` class in the appropriate `pyatem/messages/<feature>.py`
   file (likely `fairlight.py` for the Fairlight dynamics) using the
   DSL field types.
4. Add operations in `pyatem/operations.py` and a facade method on
   `pyatem.atem.ATEM`.
5. Add a `_build_*` and `_apply_*` in `pyatem/profile.py`.
6. The `ApplyOptions.restore_audio` flag already covers Fairlight as a
   whole; no new option needed.

---

## Cross-reference

| Topic | File |
|---|---|
| `<Profile>` XML schema | [`PROFILE_FORMAT.md`](PROFILE_FORMAT.md) |
| Macro wire protocol & bytecode format | [`MACRO_FORMAT.md`](MACRO_FORMAT.md) |
| Profile module source | [`profile.py`](profile.py) |
| Macro module source | [`macrotransfer.py`](macrotransfer.py) |
| Django views | [`../atem_control/profile/views.py`](../atem_control/profile/views.py), [`../atem_control/profile/export.py`](../atem_control/profile/export.py) |
| Django URLs | [`../atem_control/urls.py`](../atem_control/urls.py) |
| Frontend JS | [`../atem_control/static/js/atem_profile.js`](../atem_control/static/js/atem_profile.js) |
| Dialog template | [`../atem_control/templates/control.html`](../atem_control/templates/control.html) (lines 1814–1970) |
| Tests | [`../tests/unit/test_profile_*.py`](../tests/unit/), [`../tests/unit/test_apply_macros.py`](../tests/unit/test_apply_macros.py), [`../tests/unit/test_macro_*.py`](../tests/unit/) |
| Live-ATEM smoke / discovery scripts | [`../av_server/tools/`](../av_server/tools/) |
