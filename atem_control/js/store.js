/* store.js — the Alpine store ('atem') behind every $store.atem binding
 * on the control page.
 *
 * Registered on alpine:init (the bundle's script tag sits above the
 * deferred Alpine tag — load order is load-bearing). Holds the reactive
 * state literal (the updateState merge-whitelist: a top-level
 * build_full_state key NOT pre-declared there is dropped, loudly), the
 * updateState merge engine with its realtime-slider echo guards, and the
 * per-feature command methods the templates call. The slider method
 * quadruplets are generated — see sliders.js; its descriptor paths and
 * updateState's guard-capture lists must stay in sync (SNAP-BACK WARNING
 * in that file).
 *
 * Getters stay in THIS literal on purpose: object spread evaluates
 * getters instead of copying them, so any future carve-out of store
 * methods into a spread part must take methods and plain data only.
 */

import { buildSliderHandlers } from './sliders.js';
import { mediaPoolStore } from './media_pool.js';

document.addEventListener('alpine:init', () => {
    // ATEM inventory ([{name, ip, location}]) rendered by the view into a
    // json_script block in control.html. Backs the header quick-connect
    // input's name-or-IP suggestion dropdown.
    let atemEquipment = [];
    try {
        const eqEl = document.getElementById('atem-equipment-data');
        if (eqEl) atemEquipment = JSON.parse(eqEl.textContent) || [];
    } catch (e) { /* suggestions just stay empty */ }

    Alpine.store('atem', {
        // Connection state
        connected: false,
        // ``stateReady`` flips true on the FIRST atem_state arrival
        // (see updateState). Templates that don't want to render against
        // the initial default values gate their x-show on
        // ``connected && stateReady`` instead of just ``connected``. This
        // eliminates the visible flash when the first real state arrives
        // and bindings update wholesale (Issue #15).
        stateReady: false,
        // Which M/E this page renders. All per-M/E reads route through
        // state.mes[activeMe] (Stage 3C); the command send path stamps
        // every payload with it (Stage 4A — see ATEMControl.cmd), and the
        // dragRegistry paths embed it. Switch via setActiveMe(), never by
        // assigning directly — the rate inputs need a resync on switch.
        activeMe: 0,
        // Set true when the operator clicks the in-page Disconnect button.
        // ``connection_status`` then knows to redirect WITHOUT stashing a
        // "lost connection" message on the Connect page.
        _userInitiatedDisconnect: false,
        currentIP: '---.---.---.---',
        currentName: '',
        // Live program-monitor (see atem_pgm_monitor.js). pgmStreamUrl is the
        // room's program stream, resolved from the IP via the lookup-name
        // API; null when this ATEM has no stream mapping (the Live PGM button
        // hides). pgmMonitorOpen mirrors the floating panel's open state so
        // the header button can show an active style.
        pgmStreamUrl: null,
        pgmMonitorOpen: false,
        statusText: '',  // empty so the loading overlay shows
                        // "Connecting…" instead of "Disconnected"
                        // before the first connection_status arrives.

        // Profile-save media-pool capture progress. Lives on the store
        // (not the page x-data) because the WebSocket message handler
        // is in atem_control.js and only has access to the store, not
        // to the page-level x-data scope where the rest of the modal
        // state (profileBusy etc.) lives. The save dialog's Alpine
        // template reads these via ``$store.atem.profileCapture*``.
        profileCaptureSession: '',
        profileCaptureProgress: null,

        // HyperDeck transport modal. Open flag + the deck's clips/status live
        // here (driven by window.AtemHyperdeck over the /atem/hyperdeck/ HTTP
        // endpoints — not the ATEM WebSocket). decks = the ATEM-bound decks
        // from state.hyperdecks; deckIp = the one currently selected.
        hyperdeckOpen: false,
        // decks bound on the ATEM but missing from the equipment list (set by
        // the connect-time reconcile); shown as a notice in the settings panel.
        hyperdeckUnknownDecks: [],
        hyperdeck: {
            deckIp: null,
            decks: [],
            name: null,         // deck's Equipment-list name, if known
            model: '',          // e.g. "HyperDeck Studio HD Mini"
            videoFormat: '',    // e.g. "1080p25" — for timecode fps
            slots: [],          // [{id, name, status, connected}]
            clips: [],          // [{clip_id, name, duration, slot, slot_name}]
            transport: {},
            loop: false,
            loading: false,
            error: null,
            sampleMs: 0,        // when `transport` was sampled (playhead interpolation)
            tick: 0,            // render-tick bumped by atem_hyperdeck.js while playing
        },

        // Media pool — this feature's store slice (data + methods) lives
        // in media_pool.js and is spread in here.
        ...mediaPoolStore,

        // ATEM state - reactive with DVE support
        state: {
            // Every top-level key build_full_state can send MUST be
            // pre-declared here — updateState merges only declared keys
            // and shouts (reportUndeclaredStateKey) on anything else.
            // The payload's own liveness marker; also the ONLY key on the
            // disconnected payload ({is_connected: false}).
            is_connected: false,
            // Latched last-run macro ({index, name}) from MRPr watching.
            // Declared so it lands; no UI reads it yet.
            lastRunMacro: { index: -1, name: null },
            // The switcher's self-assigned name (ATEM Setup / WhoI, model
            // string fallback) — upstreamed from the WebATEM extraction.
            // No monorepo UI reads it yet; the standalone build shows it
            // in the header.
            atem_name: '',
            // Per-M/E state lives under mes[me] (Stage 3C). One entry per
            // M/E; every M/E is populated since the Stage 4A multi-M/E
            // flip. Must exist in the initial state so the updateState
            // merge (which only copies already-defined keys) picks up
            // mes (Guardrail 7). The entry's sub-shapes are the pre-3C
            // flat program/preview/usk/ftb/transition defaults, unchanged.
            mes: [{
            program: null,
            preview: null,
            usk: {
                states: [false, false, false, false],
                types: [0, 0, 0, 0],  // ADDED: Types array to fix USK state detection
                data: [
                    {
                        type: 0, fill_source: 0, key_source: 0,
                        luma_clip: 0.0, luma_gain: 100.0, luma_invert: false, luma_pre_multiplied: false,
                        chroma_hue: 0.0, chroma_gain: 0.0, chroma_lift: 0.0, chroma_narrow: false, chroma_y_suppress: 0.0,
                        pattern_style: 0, pattern_size: 50.0, pattern_symmetry: 50.0, pattern_softness: 0.0, 
                        pattern_invert: false, pattern_position_x: 0.5, pattern_position_y: 0.5,
                        mask_enabled: false, mask_top: 9.0, mask_bottom: -9.0, mask_left: -16.0, mask_right: 16.0,
                        dve_position_x: 0.0, dve_position_y: 0.0, dve_size_x: 1.0, dve_size_y: 1.0, dve_rotation: 0.0,
                        fly_enabled: false, dve_shadow: false, dve_light_direction: 0.0, dve_light_altitude: 25.0
                    },
                    {
                        type: 0, fill_source: 0, key_source: 0,
                        luma_clip: 0.0, luma_gain: 100.0, luma_invert: false, luma_pre_multiplied: false,
                        chroma_hue: 0.0, chroma_gain: 0.0, chroma_lift: 0.0, chroma_narrow: false, chroma_y_suppress: 0.0,
                        pattern_style: 0, pattern_size: 50.0, pattern_symmetry: 50.0, pattern_softness: 0.0, 
                        pattern_invert: false, pattern_position_x: 0.5, pattern_position_y: 0.5,
                        mask_enabled: false, mask_top: 9.0, mask_bottom: -9.0, mask_left: -16.0, mask_right: 16.0,
                        dve_position_x: 0.0, dve_position_y: 0.0, dve_size_x: 1.0, dve_size_y: 1.0, dve_rotation: 0.0,
                        fly_enabled: false, dve_shadow: false, dve_light_direction: 0.0, dve_light_altitude: 25.0
                    },
                    {
                        type: 0, fill_source: 0, key_source: 0,
                        luma_clip: 0.0, luma_gain: 100.0, luma_invert: false, luma_pre_multiplied: false,
                        chroma_hue: 0.0, chroma_gain: 0.0, chroma_lift: 0.0, chroma_narrow: false, chroma_y_suppress: 0.0,
                        pattern_style: 0, pattern_size: 50.0, pattern_symmetry: 50.0, pattern_softness: 0.0, 
                        pattern_invert: false, pattern_position_x: 0.5, pattern_position_y: 0.5,
                        mask_enabled: false, mask_top: 9.0, mask_bottom: -9.0, mask_left: -16.0, mask_right: 16.0,
                        dve_position_x: 0.0, dve_position_y: 0.0, dve_size_x: 1.0, dve_size_y: 1.0, dve_rotation: 0.0,
                        fly_enabled: false, dve_shadow: false, dve_light_direction: 0.0, dve_light_altitude: 25.0
                    },
                    {
                        type: 0, fill_source: 0, key_source: 0,
                        luma_clip: 0.0, luma_gain: 100.0, luma_invert: false, luma_pre_multiplied: false,
                        chroma_hue: 0.0, chroma_gain: 0.0, chroma_lift: 0.0, chroma_narrow: false, chroma_y_suppress: 0.0,
                        pattern_style: 0, pattern_size: 50.0, pattern_symmetry: 50.0, pattern_softness: 0.0, 
                        pattern_invert: false, pattern_position_x: 0.5, pattern_position_y: 0.5,
                        mask_enabled: false, mask_top: 9.0, mask_bottom: -9.0, mask_left: -16.0, mask_right: 16.0,
                        dve_position_x: 0.0, dve_position_y: 0.0, dve_size_x: 1.0, dve_size_y: 1.0, dve_rotation: 0.0,
                        fly_enabled: false, dve_shadow: false, dve_light_direction: 0.0, dve_light_altitude: 25.0
                    }
                ]
            },
            ftb: { active: false, disabled: false, in_transition: false },
            transition: { 
                in_transition: false,
                style: 0,
                rate: '1:00',
                frames_remaining: 0,
                selection: { background: true, key1: false, key2: false, key3: false, key4: false },
                wipe_pattern: 0,
                // Individual transition rates
                mix_rate: '1:00',
                dip_rate: '1:00',
                wipe_rate: '1:00',
                dve_rate: '1:00',
                stinger_rate: '1:00',
                // Dip source
                dip_source: 0,
                // Wipe settings
                wipe_fill_source: 0,
                wipe_flip_flop: false,
                wipe_position_x: 0.5,
                wipe_position_y: 0.5,
                wipe_reverse: false,
                wipe_softness: 0.0,
                wipe_symmetry: 50.0,
                wipe_width: 0.0,
                // DVE settings - CORRECTED default style to 24
                dve: {
                    fill_source: 1,
                    key_source: 1,
                    enable_key: false,
                    clip: 0.0,
                    gain: 100.0,
                    pre_multiplied: false,
                    invert_key: false,
                    style: 24, // Default to Push Top Left (corrected from 28)
                    reverse: false,
                    flip_flop: false
                },
                // Stinger settings
                stinger: {
                    source: 3010,
                    clip_duration: 150,
                    trigger_point: 34,
                    mix_rate: 5,
                    pre_roll: 48,
                    clip: 50.0,
                    gain: 70.0,
                    pre_multiplied: true,
                    invert_key: false,
                    clip_duration_str: '6:00',
                    trigger_point_str: '1:09',
                    mix_rate_str: '0:05',
                    pre_roll_str: '1:18'
                }
            },
            }],
            // One entry per DSK (Stage 3B dsk → dsks[] restructure). Must
            // exist in the initial state so the updateState merge (which
            // only copies already-defined keys) picks up dsks (Guardrail 7).
            // Single default entry = the pre-restructure single-DSK shape;
            // replaced by the real per-DSK array on first snapshot.
            dsks: [
                {
                    on_air: false,
                    rate: '1:00',
                    rate_str: '1:00',
                    in_transition: false,
                    is_auto_transitioning: false,
                    frames_remaining: 0,
                    // Fill and Key Sources
                    fill_source: 3020,    // Media Player 2
                    key_source: 3021,     // Media Player 2 Key
                    // Mask parameters
                    mask_enabled: false,
                    mask_top: 9.0,
                    mask_bottom: -9.0,
                    mask_left: -16.0,
                    mask_right: 16.0,
                    // Pre-multiplied key parameters
                    pre_multiplied: false,
                    clip: 22.0,
                    gain: 30.0,
                    invert_key: false
                }
            ],
            macros: [],
            colorGenerators: {
                0: { hue: 0.0, saturation: 100.0, luma: 50.0, hex: '#ff0000' },
                1: { hue: 120.0, saturation: 100.0, luma: 50.0, hex: '#00ff00' }
            },
            auxOutputs: { aux1: 0, aux2: 0, aux3: 0, aux4: 0, aux5: 0, aux6: 0 },
            // Must exist in the initial state so the updateState merge (which
            // only copies already-defined keys) picks up hyperdecks. Replaced
            // by the real 10-slot list (build_full_state) on first snapshot.
            hyperdecks: [],
            // Must exist in the initial state so the updateState merge picks
            // up topology (the Stage 3A USK x-for renders zero sections
            // without it). Shape mirrors _build_topology's empty result;
            // all-zero counts mean the dynamic-count surfaces render nothing
            // until the real capability counts arrive on first snapshot.
            topology: {
                meCount: 0,
                usksPerMe: [],
                dsks: 0,
                auxOutputs: 0,
                inputs: 0,
                outputs: 0,
                mediaPlayers: 0,
                mediaPoolStills: 0,
                mediaPoolClips: 0,
                colorGenerators: 0,
                multiviewWindows: 0,
                fairlightStrips: 0,
                macros: 0
            },
            // Must exist in the initial state so the merge in updateState
            // (which only copies keys that are already defined) picks it
            // up. Default 25 fps is the safe handshake fallback before
            // the first state arrives — gets replaced by the real ATEM
            // video-mode rate (30 / 50 / 60 etc.) on first snapshot.
            videoMode: { format: '', id: -1, fps: 25 },
            // Available video modes for the settings menu Video Mode
            // dropdown. Empty until the _VMC capability field arrives;
            // populated by build_full_state via available_video_modes().
            videoModes: [],
            // Renamable inputs for the Input Labels section, grouped
            // into Software Control's three tabs: inputs (cameras),
            // outputs (M/E, aux, multiview), media (color generators,
            // media players). Each bucket is an array of
            // {source, long, short}. Empty until the InPr field arrives.
            inputLabels: { inputs: [], outputs: [], media: [] },
            // Full source list for dynamic source-picker dropdowns (USK
            // fill/key, DSK fill/key, transition fills, aux outputs).
            // Each entry carries available_* flags from InPr so
            // ``sourcesFor(ctx)`` can filter per dropdown context.
            // Empty until InPr arrives.
            sources: [],
            // Fairlight audio. ``present: false`` is the loading-gate the
            // template watches — the Fairlight handshake arrives ~hundreds
            // of ms after the rest of the state dump. Default empty
            // strips/master so the template can read fields safely before
            // the first state push lands.
            audio: {
                present: false,
                master: { present: false, volume_db: 0.0, eq_enable: false, eq_gain_db: 0.0,
                          dynamics_makeup_db: 0.0, afv: false, eq_bands: [] },
                strips: [],
                headphones: { present: false, volume_db: 0.0, unmuted: true },
                solo: { any_soloed: false, strip_id: null }
            }
        },
                
        // Quick-connect name-or-IP suggestions (header input). ``equipment``
        // is the ATEM inventory from the json_script block; the dropdown
        // renders quickConnectMatches, keyboard nav moves the highlight.
        equipment: atemEquipment,
        quickConnectOpen: false,
        quickConnectHighlight: 0,

        get quickConnectMatches() {
            return this._atemEquipmentMatches(this.inputs.quickConnectIP).slice(0, 8);
        },

        _queryTokens(query) {
            return (query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
        },

        _nameMatchesAll(e, tokens) {
            const name = e.name.toLowerCase();
            return tokens.every(t => name.includes(t));
        },

        // One token against one row: substring on name/location, but IP
        // hits must align to an octet boundary — "81.1" narrows 81.1xx,
        // while a room-number token like "1.1" can't ghost-match the
        // middle of 81.140.
        _tokenMatches(e, t) {
            return e.name.toLowerCase().includes(t) ||
                (e.location || '').toLowerCase().includes(t) ||
                ('.' + e.ip).includes('.' + t);
        },

        // Token search: every space-separated token must hit somewhere
        // across name + IP + location, so "2 north" finds
        // "Studio 2 - North Hall". Rows whose NAME alone satisfies every
        // token rank first.
        _atemEquipmentMatches(query) {
            const tokens = this._queryTokens(query);
            if (!tokens.length) return [];
            const out = this.equipment.filter(e =>
                tokens.every(t => this._tokenMatches(e, t))
            );
            out.sort((a, b) => this._nameMatchesAll(b, tokens) - this._nameMatchesAll(a, tokens));
            return out;
        },

        // UPDATED: Input values including individual transition rates and stinger inputs
        inputs: {
            quickConnectIP: '',
            transitionRate: '1:00',
            // Quick-panel per-DSK rate inputs (index = 0-based DSK)
            dskRates: ['1:00', '1:00'],
            ftbRate: '1:00',
            // Individual transition rate inputs
            mixRate: '1:00',
            dipRate: '1:00',
            wipeRate: '1:00',
            dveRate: '1:00',
            stingerRate: '1:00',
            // Stinger time inputs
            stingerClipDuration: '6:00',
            stingerTriggerPoint: '1:09',
            stingerMixRate: '0:05',
            stingerPreRoll: '1:18',
            // Flying-key DVE rate, per 0-based keyer ("1:00" strings).
            // Populated by _syncMeRateInputs from usk.data[k].dve_rate;
            // the four coexisting key-type panels share one entry per
            // keyer (their inputs are x-show alternatives).
            uskDveRates: [],
        },
        
        // NEW: DVE state management
        dve: {
            currentCategory: 'push', // Current category (push, squeeze, etc.)
            currentCategoryIndex: 0
        },
        
        // Color Generator settings
        colorGen: {
            selectedGenerator: 0,  // Currently selected generator (0 or 1)
            hue: 0.0,
            saturation: 100.0,
            luma: 50.0,
        },
        
        // Macro pagination
        macros: {
            currentPage: 0,
            perPage: 8,
            total: 100
        },

        // Parse rate method for stinger time inputs
        parseRate(rateStr) {
            return ATEMControl.parseRate(rateStr);
        },

        setUSKType(uskIndex, keyType) {
            const typeMap = {
                'Luma': 0,
                'Chroma': 1, 
                'Pattern': 2,
                'DVE': 3
            };
            
            const typeValue = typeof keyType === 'string' ? typeMap[keyType] : keyType;
            this.send('set_usk_type', {key_index: uskIndex, key_type: typeValue});
        },

        // Source-picker filter for dropdowns. ``ctx`` selects the
        // filter rule:
        //   'aux'        — destinations valid for an aux output
        //                  (uses available_aux from InPr)
        //   'key_source' — sources valid as a keyer key source
        //                  (uses available_key_source from InPr)
        //   'fill'       — sources valid as a keyer fill / DSK fill /
        //                  transition fill: cameras, black, color
        //                  bars, color generators, media player fills.
        //                  Excludes media player KEY channels, masks,
        //                  M/E outputs, etc. Filtered by port_type so
        //                  the result matches what the existing
        //                  hardcoded dropdowns showed (port_type ≤ 4).
        // Returns array of {source, long, short, ...}, sorted by id.
        sourcesFor(ctx) {
            const all = this.state?.sources || [];
            if (ctx === 'aux') return all.filter(s => s.available_aux);
            if (ctx === 'key_source') return all.filter(s => s.available_key_source);
            if (ctx === 'fill') return all.filter(s => s.port_type <= 4);
            return all;
        },

        // Program / Preview bus rows. The button set comes from the
        // switcher's real source list (state.sources, via InPr) instead
        // of a hardcoded table, so a 10-input unit and a 40-input
        // Constellation both render exactly what they have. Bus-eligible
        // sources: externals (port_type 0) first in input order, then
        // internals — black, bars, color gens, media-player fills,
        // SuperSource. MP key channels (5) and M/E outputs are excluded.
        busAllSources() {
            // M/E outputs (port_type 128) join the bus per the wire's
            // re-entry rules: each source carries available_me<N> flags
            // saying which M/E buses may take it (ASC's M/E 3 / M/E 4
            // bus buttons on multi-M/E units). Keyed to the active M/E.
            const meFlag = 'available_me' + (this.activeMe + 1);
            const ok = (this.state?.sources || [])
                .filter(s => s.port_type <= 4 || s.port_type === 6
                    || (s.port_type === 128 && s[meFlag]));
            return ok.filter(s => s.port_type === 0)
                .concat(ok.filter(s => s.port_type !== 0));
        },
        // ASC-style two-row bus layout: cameras lead (max 10 per row, so
        // 20 per bank), then a spacer, then the internals split across
        // the two row tails. Big units get two Shift banks with ASC's
        // pairing (BLK↔BARS, SS1↔SS2, COL1↔COL2, MP1/2↔MP3/4, M/E PGM
        // re-entries↔their PVWs) so BLK / colors / media players are
        // always reachable without shifting. Everything derives from
        // the connected model's topology — no per-model hardcoding.
        busLayout() {
            const all = this.busAllSources();
            const cams = all.filter(s => s.port_type === 0);
            const find = (id) => all.find(s => s.source === id) || null;
            const blk = find(0), bars = find(1000);
            const cols = all.filter(s => s.port_type === 3);
            const mps = all.filter(s => s.port_type === 4);
            const ss = all.filter(s => s.port_type === 6);
            const meOut = all.filter(s => s.port_type === 128);
            // BMD id space: M/E n PGM = 100n0, its PVW = 100n1.
            const mePgm = meOut.filter(s => s.source % 10 === 0);
            const mePvw = meOut.filter(s => s.source % 10 === 1);
            const c = (a) => a.filter(Boolean);

            // One-bank shape (small units): cams split evenly; middle
            // section = BLK over SS/BARS; right section = re-entry PGMs
            // + colors over MPs (+ re-entry PVWs).
            const oneR1mid = c([blk]);
            const oneR2mid = c([...ss, bars]);
            const oneR1x = c([...mePgm, ...cols]);
            const oneR2x = c([...mps, ...mePvw]);
            const oneHalf = Math.ceil(cams.length / 2);
            const oneWidth = oneHalf + Math.max(oneR1mid.length, oneR2mid.length)
                + Math.max(oneR1x.length, oneR2x.length);
            if (cams.length <= 20 && oneWidth <= 14) {
                return { banked: false, banks: [{
                    r1cams: cams.slice(0, oneHalf), r2cams: cams.slice(oneHalf),
                    r1mid: oneR1mid, r2mid: oneR2mid, r1x: oneR1x, r2x: oneR2x,
                }] };
            }
            // Two banks, ASC pairing.
            return { banked: true, banks: [
                { r1cams: cams.slice(0, 10), r2cams: cams.slice(10, 20),
                  r1mid: c([blk]), r2mid: c([ss[0]]),
                  r1x: c([...mePgm]), r2x: c([cols[0], mps[0], mps[1]]) },
                { r1cams: cams.slice(20, 30), r2cams: cams.slice(30, 40),
                  r1mid: c([bars]), r2mid: c([ss[1]]),
                  r1x: c([...mePvw]), r2x: c([cols[1], mps[2], mps[3]]) },
            ] };
        },
        // Renderable geometry for the current bank: two rows of cells
        // (source | null); the two nulls between segments line up with
        // the grid's section-gap tracks (cams | BLK/BARS | COL/MP).
        busGeom() {
            const L = this.busLayout();
            const bank = L.banks[L.banked && this.busShifted() ? 1 : 0];
            const camCols = Math.max(bank.r1cams.length, bank.r2cams.length, 1);
            const midCols = Math.max(bank.r1mid.length, bank.r2mid.length, 1);
            const xCols = Math.max(bank.r1x.length, bank.r2x.length, 1);
            const pad = (a, n) => a.concat(Array(Math.max(0, n - a.length)).fill(null));
            const row = (camsArr, midArr, xArr) =>
                pad(camsArr, camCols).concat([null], pad(midArr, midCols), [null], pad(xArr, xCols));
            return {
                camCols, midCols, xCols,
                rows: [row(bank.r1cams, bank.r1mid, bank.r1x),
                       row(bank.r2cams, bank.r2mid, bank.r2x)],
            };
        },
        // USK bank columns. The ON AIR row renders one button per USK the M/E
        // reports and the BKGD/KEY row one per USK in the topology; they live
        // in separate grids that have to line up, so both take the wider of
        // the two plus the leading BKGD / spacer cell. (On real hardware the
        // two agree — they differ only under a grafted topology, e2e --atem.)
        uskCols() {
            const states = this.state.mes?.[this.activeMe]?.usk?.states?.length ?? 0;
            const topo = this.state.topology?.usksPerMe?.[this.activeMe] ?? 0;
            return Math.max(states, topo, 1) + 1;
        },
        busShiftHeld: false,
        busShiftLatched: false,
        busBanked() { return this.busLayout().banked; },
        busShifted() { return this.busBanked() && (this.busShiftHeld || this.busShiftLatched); },
        toggleBusShift() { this.busShiftLatched = !this.busShiftLatched; },

        setUSKFillSource(uskIndex, source) {
            this.send('set_usk_fill_source', {key_index: uskIndex, source: parseInt(source)});
        },

        setUSKKeySource(uskIndex, source) {
            this.send('set_usk_key_source', {key_index: uskIndex, source: parseInt(source)});
        },

        // FIXED: Helper to get current USK type - now uses types array first
        getUSKType(uskIndex) {
            return this.state.mes[this.activeMe].usk?.types?.[uskIndex] ?? 
                   this.state.mes[this.activeMe].usk?.data?.[uskIndex]?.type ?? 0;
        },

        // Realtime-slider drag-handler quadruplets (USK luma / pattern /
        // DVE / advanced-chroma, DSK clip+gain, stinger + DVE-transition
        // clip+gain) are generated from the descriptor tables by
        // buildSliderHandlers() — see the factory above the store
        // registration. Generated names and signatures are identical to
        // the formerly hand-written handlers, so template bindings are
        // unchanged.
        ...buildSliderHandlers(),

        setUSKLumaInvert(uskIndex, invert) {
            this.send('set_usk_luma_invert', {key_index: uskIndex, invert: invert});
        },

        setUSKLumaPreMultiplied(uskIndex, preMultiplied) {
            this.send('set_usk_luma_pre_multiplied', {key_index: uskIndex, pre_multiplied: preMultiplied});
        },

        // Legacy chroma methods (hue/gain/lift/narrow/y_suppress) — removed.
        // Modern ATEM firmware uses the advanced-chroma keyer exclusively;
        // those controls live in the Advanced Chroma tab below.

        // Chroma Sample methods - corrected command names
        setUSKChromaSample(uskIndex, enabled) {
            this.send('set_usk_chroma_sample', {
                key_index: uskIndex,
                enabled: enabled
            });
        },

        setUSKChromaSamplePosition(uskIndex, x, y) {
            this.send('set_usk_chroma_sample_position', {
                key_index: uskIndex,
                x: parseFloat(x),
                y: parseFloat(y)
            });
        },

        setUSKChromaSampleSize(uskIndex, size) {
            this.send('set_usk_chroma_sample_size', {
                key_index: uskIndex,
                size: parseFloat(size)
            });
        },

        setUSKChromaPreview(uskIndex, preview) {
            this.send('set_usk_chroma_preview', {
                key_index: uskIndex,
                preview: preview
            });
        },

        // YCbCr to RGB conversion
        ycbcrToRgb(y, cb, cr) {
            // Handle null/undefined/zero values - return black for no signal
            if (y === undefined || y === null || (y === 0 && cb === 0 && cr === 0)) {
                return '#000000'; // Black for no input
            }
            
            // YCbCr values are normalized: Y (0-1), Cb/Cr (-1 to 1)
            // Convert to standard YCbCr range
            const Y = y * 255;
            const Cb = (cb * 127) + 128;
            const Cr = (cr * 127) + 128;
            
            // YCbCr to RGB conversion (ITU-R BT.601)
            let R = Y + 1.402 * (Cr - 128);
            let G = Y - 0.344136 * (Cb - 128) - 0.714136 * (Cr - 128);
            let B = Y + 1.772 * (Cb - 128);
            
            // Clamp values to 0-255
            R = Math.max(0, Math.min(255, Math.round(R)));
            G = Math.max(0, Math.min(255, Math.round(G)));
            B = Math.max(0, Math.min(255, Math.round(B)));
            
            // Return as hex color
            const toHex = (val) => {
                const hex = val.toString(16);
                return hex.length === 1 ? '0' + hex : hex;
            };
            
            return `#${toHex(R)}${toHex(G)}${toHex(B)}`;
        },

        // Helper to get the current chroma sample color
        getChromaSampleColor(uskIndex) {
            const sampledColor = this.state.mes[this.activeMe].usk?.data?.[uskIndex]?.chroma_sampled_color;
            if (!sampledColor) {
                return '#000000'; // Default black if no sample
            }
            
            // Check if we have valid color data
            if (!sampledColor.y && !sampledColor.cb && !sampledColor.cr) {
                return '#000000'; // Black for no signal
            }
            
            return this.ycbcrToRgb(
                sampledColor.y || 0,
                sampledColor.cb || 0,
                sampledColor.cr || 0
            );
        },

        // USK Pattern methods
        setUSKPatternStyle(uskIndex, pattern) {
            this.send('set_usk_pattern_style', {key_index: uskIndex, pattern: parseInt(pattern)});
        },

        getUSKPatternButtonClass(uskIndex, patternIndex) {
            const currentPattern = parseInt(this.state.mes[this.activeMe].usk?.data?.[uskIndex]?.pattern_style) || 0;
            const targetPattern = parseInt(patternIndex);
            const isActive = currentPattern === targetPattern;
            return isActive ? 'usk-pattern-button usk-active' : 'usk-pattern-button';
        },

        setUSKPatternInvert(uskIndex, invert) {
            this.send('set_usk_pattern_invert', {key_index: uskIndex, invert: invert});
        },

        setUSKPatternPositionX(uskIndex, positionX) {
            this.send('set_usk_pattern_position_x', {key_index: uskIndex, positionX: parseFloat(positionX)});
        },

        setUSKPatternPositionY(uskIndex, positionY) {
            this.send('set_usk_pattern_position_y', {key_index: uskIndex, positionY: parseFloat(positionY)});
        },

        // USK Mask methods
        setUSKMaskEnabled(uskIndex, enabled) {
            this.send('set_usk_mask_enabled', {key_index: uskIndex, enabled: enabled});
        },

        setUSKMaskTop(uskIndex, top) {
            this.send('set_usk_mask_top', {key_index: uskIndex, top: parseFloat(top)});
        },

        setUSKMaskBottom(uskIndex, bottom) {
            this.send('set_usk_mask_bottom', {key_index: uskIndex, bottom: parseFloat(bottom)});
        },

        setUSKMaskLeft(uskIndex, left) {
            this.send('set_usk_mask_left', {key_index: uskIndex, left: parseFloat(left)});
        },

        setUSKMaskRight(uskIndex, right) {
            this.send('set_usk_mask_right', {key_index: uskIndex, right: parseFloat(right)});
        },

        // USK DVE methods
        setUSKDVEPositionX(uskIndex, positionX) {
            this.send('set_usk_dve_position_x', {key_index: uskIndex, positionX: parseFloat(positionX)});
        },

        setUSKDVEPositionY(uskIndex, positionY) {
            this.send('set_usk_dve_position_y', {key_index: uskIndex, positionY: parseFloat(positionY)});
        },

        setUSKDVESizeX(uskIndex, sizeX) {
            this.send('set_usk_dve_size_x', {key_index: uskIndex, sizeX: parseFloat(sizeX)});
        },

        setUSKDVESizeY(uskIndex, sizeY) {
            this.send('set_usk_dve_size_y', {key_index: uskIndex, sizeY: parseFloat(sizeY)});
        },

        setUSKFlyEnabled(uskIndex, enabled) {
            this.send('set_usk_fly_enabled', {key_index: uskIndex, enabled: enabled});
        },

        // Aux Control Methods — route every aux output to ME 1 Program
        // (10010). Lives here because Alpine can't parse statement loops
        // inside directive attributes; the Outputs section's button calls
        // this instead of an inline for.
        setAllAuxToProgram() {
            const count = this.state.topology?.auxOutputs ?? 0;
            for (let ch = 0; ch < count; ch++) {
                this.send('set_aux_output', { aux_channel: ch, input_source: 10010 });
            }
        },

        // DSK Control Methods — first arg is the 0-based DSK index
        // (Stage 3B: the settings DSK sections render via x-for off
        // topology.dsks).
        setDSKFillSource(dsk, source) {
            this.send('set_dsk_fill_source', { dsk: dsk, source: parseInt(source) });
        },

        setDSKKeySource(dsk, source) {
            this.send('set_dsk_key_source', { dsk: dsk, source: parseInt(source) });
        },

        setDSKMaskEnabled(dsk, enabled) {
            this.send('set_dsk_mask_enabled', { dsk: dsk, enabled: enabled });
        },

        setDSKMaskTop(dsk, top) {
            this.send('set_dsk_mask_top', { dsk: dsk, top: parseFloat(top) });
        },

        setDSKMaskBottom(dsk, bottom) {
            this.send('set_dsk_mask_bottom', { dsk: dsk, bottom: parseFloat(bottom) });
        },

        setDSKMaskLeft(dsk, left) {
            this.send('set_dsk_mask_left', { dsk: dsk, left: parseFloat(left) });
        },

        setDSKMaskRight(dsk, right) {
            this.send('set_dsk_mask_right', { dsk: dsk, right: parseFloat(right) });
        },

        setDSKPreMultiplied(dsk, preMultiplied) {
            this.send('set_dsk_pre_multiplied', { dsk: dsk, pre_multiplied: preMultiplied });
        },

        setDSKInvertKey(dsk, invertKey) {
            this.send('set_dsk_invert_key', { dsk: dsk, invert_key: invertKey });
        },

        // FIXED: DVE methods that use ATEM state instead of local state
        
        // DVE Style mappings
        dveStyleMappings: {
            push: {
                base: 24, // CORRECTED from 28
                positions: [24, 25, 26, 27, 28, 29, 30, 31], // grid positions 0,1,2,3,5,6,7,8
                name: 'Push'
            },
            squeeze: {
                base: 16, 
                positions: [16, 17, 18, 19, 20, 21, 22, 23], // grid positions 0,1,2,3,5,6,7,8
                name: 'Squeeze'
            }
        },

        // CORRECTED: Get current DVE category from ATEM state - Fixed Push range to 24-31
        getCurrentDVECategoryFromATEM() {
            const style = this.state.mes[this.activeMe].transition?.dve?.style;
            if (!style) return 'Unknown';
            
            // Check if it's a push style (24-31)
            if (style >= 24 && style <= 31) {
                return 'Push';
            }
            // Check if it's a squeeze style (16-23) 
            if (style >= 16 && style <= 23) {
                return 'Squeeze';
            }
            
            return 'Other';
        },

        // Get current DVE style name from ATEM state
        getCurrentDVEStyleNameFromATEM() {
            const style = this.state.mes[this.activeMe].transition?.dve?.style;
            if (!style) return 'None';
            
            const category = this.getCurrentDVECategoryFromATEM();
            const position = this.getDVEGridPositionFromStyle(style);
            
            if (position === -1) return `Style ${style}`;
            
            const directions = ['Top Left', 'Top', 'Top Right', 'Left', 'Center', 'Right', 'Bottom Left', 'Bottom', 'Bottom Right'];
            return `${category} ${directions[position]}`;
        },

        // Check if current category matches the given category
        isCurrentDVECategoryFromATEM(category) {
            return this.getCurrentDVECategoryFromATEM().toLowerCase() === category.toLowerCase();
        },

        // CORRECTED: Get grid position from DVE style number - Fixed Push range to 24-31
        getDVEGridPositionFromStyle(style) {
            // Push styles 24-31 map to positions 0,1,2,3,5,6,7,8
            if (style >= 24 && style <= 31) {
                const arrayIndex = style - 24; // 0-7
                // Convert array index back to grid position
                return arrayIndex >= 4 ? arrayIndex + 1 : arrayIndex;
            }
            // Squeeze styles 16-23 map to positions 0,1,2,3,5,6,7,8  
            if (style >= 16 && style <= 23) {
                const arrayIndex = style - 16; // 0-7
                // Convert array index back to grid position  
                return arrayIndex >= 4 ? arrayIndex + 1 : arrayIndex;
            }
            return -1; // Unknown style
        },

        // Get button class for grid position based on ATEM state
        getDVEGridButtonClassFromATEM(gridPosition) {
            const currentStyle = this.state.mes[this.activeMe].transition?.dve?.style;
            const currentGridPosition = this.getDVEGridPositionFromStyle(currentStyle);
            
            const baseClass = "aspect-square border-2 rounded flex items-center justify-center transition-all duration-200 hover:scale-105 active:scale-95 min-h-8 min-w-8";
            
            if (currentGridPosition === gridPosition) {
                return `${baseClass} border-primary bg-primary text-primary-content shadow-lg`;
            } else {
                return `${baseClass} border-base-300 bg-base-100 text-base-content hover:border-primary hover:bg-base-200`;
            }
        },

        // CORRECTED: Select DVE grid position based on current category from ATEM - Fixed Push range to 24-31
        selectDVEGridPositionFromATEM(gridPosition) {
            const currentCategory = this.getCurrentDVECategoryFromATEM().toLowerCase();
            
            // Convert grid position to sequential array index (accounting for skipped center position 4)
            let arrayIndex = gridPosition;
            if (gridPosition > 4) {
                arrayIndex = gridPosition - 1; // Adjust for skipped center position
            }
            
            let styleToSet;
            
            if (currentCategory === 'push') {
                styleToSet = 24 + arrayIndex; // Push styles: 24, 25, 26, 27, 28, 29, 30, 31
            } else if (currentCategory === 'squeeze') {
                styleToSet = 16 + arrayIndex; // Squeeze styles: 16, 17, 18, 19, 20, 21, 22, 23
            } else {
                // Default to push if unknown category
                styleToSet = 24 + arrayIndex;
            }
            
            console.log(`Grid ${gridPosition} → Array ${arrayIndex} → Style ${styleToSet} (${currentCategory})`); // Debug log
            this.send('set_dve_style', { style: styleToSet });
        },

        // CORRECTED: Switch to specific DVE category - Fixed Push default to 24
        switchToDVECategory(category) {
            let defaultStyle;
            
            if (category.toLowerCase() === 'push') {
                defaultStyle = 24; // Push top-left
            } else if (category.toLowerCase() === 'squeeze') {
                defaultStyle = 16; // Squeeze top-left  
            } else {
                return; // Unknown category
            }
            
            this.send('set_dve_style', { style: defaultStyle });
        },

        // Switch DVE category (prev/next) - UPDATED to use ATEM state
        switchDVECategory(direction) {
            const currentCategory = this.getCurrentDVECategoryFromATEM().toLowerCase();
            
            if (direction === 'next') {
                if (currentCategory === 'push') {
                    this.switchToDVECategory('squeeze');
                } else {
                    this.switchToDVECategory('push');
                }
            } else if (direction === 'prev') {
                if (currentCategory === 'squeeze') {
                    this.switchToDVECategory('push');
                } else {
                    this.switchToDVECategory('squeeze');
                }
            }
        },

        // LEGACY DVE METHODS - Updated to use ATEM state but kept for compatibility
        updateDVECategory() {
            const currentStyle = this.state.mes[this.activeMe].transition?.dve?.style || 24;
            const category = ATEMControl.getCurrentDVECategory(currentStyle);
            this.dve.currentCategory = category.id;
            this.dve.currentCategoryIndex = ATEMControl.dveStyleCategories.findIndex(c => c.id === category.id);
            
            // Ensure we have a valid category index
            if (this.dve.currentCategoryIndex === -1) {
                this.dve.currentCategoryIndex = 0; // Default to Push
                this.dve.currentCategory = 'push';
            }
        },

        selectDVEGridPosition(gridIndex) {
            const currentCategory = ATEMControl.dveStyleCategories[this.dve.currentCategoryIndex];
            if (!currentCategory) return;

            const style = ATEMControl.getDVEStyleForGridPosition(currentCategory, gridIndex);
            if (style !== null) {
                this.send('set_dve_style', { style: style });
            }
        },

        getDVEGridButtonClass(gridIndex) {
            const currentCategory = ATEMControl.dveStyleCategories[this.dve.currentCategoryIndex];
            const currentStyle = this.state.mes[this.activeMe].transition?.dve?.style || 24;
            const gridStyle = ATEMControl.getDVEStyleForGridPosition(currentCategory, gridIndex);
            
            const baseClasses = 'aspect-square border border-base-300 rounded flex items-center justify-center transition-colors text-sm';
            
            // Check if this grid position has a style available
            if (!ATEMControl.hasStyleForGridPosition(currentCategory, gridIndex)) {
                return `${baseClasses} bg-base-100 opacity-30 cursor-not-allowed`;
            }
            
            // Check if this grid position's style matches the current ATEM style
            const isActive = (gridStyle === currentStyle);
            
            if (isActive) {
                return `${baseClasses} bg-primary text-primary-content border-primary shadow-lg`;
            }
            
            return `${baseClasses} bg-base-100 hover:bg-base-200 cursor-pointer`;
        },

        getCurrentDVECategoryName() {
            const category = ATEMControl.dveStyleCategories[this.dve.currentCategoryIndex];
            return category ? category.name : 'Push';
        },

        getCurrentDVEStyleName() {
            const currentStyle = this.state.mes[this.activeMe].transition?.dve?.style || 24;
            return ATEMControl.getDVEStyleName(currentStyle);
        },

        // Grid position icons based on category and position
        getDVEGridIcon(gridIndex) {
            const currentCategory = ATEMControl.dveStyleCategories[this.dve.currentCategoryIndex];
            if (!currentCategory || !ATEMControl.hasStyleForGridPosition(currentCategory, gridIndex)) {
                return '';
            }

            // Icon mapping based on grid position
            const icons = {
                0: 'bi-arrow-up-left',     // Top Left
                1: 'bi-arrow-up',          // Top
                2: 'bi-arrow-up-right',    // Top Right
                3: 'bi-arrow-left',        // Left
                5: 'bi-arrow-right',       // Right (index 4 is center, skipped)
                6: 'bi-arrow-down-left',   // Bottom Left
                7: 'bi-arrow-down',        // Bottom
                8: 'bi-arrow-down-right'   // Bottom Right
            };

            return icons[gridIndex] || '';
        },

        
        // UPDATED: Update state reactively with individual transition rate support and DVE
        updateState(newState) {
            // Incoming per-M/E state for the M/E this page renders
            // (undefined on a disconnected payload, which carries no mes).
            const newMe = newState.mes?.[this.activeMe];

            // Store previous DVE style for comparison
            const previousDVEStyle = this.state.mes[this.activeMe].transition?.dve?.style;

            // Capture pre-merge values for fields that are realtime-slider
            // guarded. The bulk merge below replaces sub-objects wholesale,
            // so per-field protection has to compare local-before vs.
            // incoming and restore the local value when shouldSkip says so.
            const guard = window.ATEMControl.dragRegistry;
            const localUskLuma = [];
            const localUskPattern = [];
            const localUskDVE = [];
            const localUskChroma = [];
            if (this.state.mes[this.activeMe].usk?.data) {
                for (let i = 0; i < this.state.mes[this.activeMe].usk.data.length; i++) {
                    const d = this.state.mes[this.activeMe].usk.data[i];
                    if (d) {
                        localUskLuma[i] = { clip: d.luma_clip, gain: d.luma_gain };
                        localUskPattern[i] = {
                            size: d.pattern_size,
                            symmetry: d.pattern_symmetry,
                            softness: d.pattern_softness,
                        };
                        localUskDVE[i] = {
                            rotation: d.dve_rotation,
                            border_hue: d.dve_border_hue,
                            border_saturation: d.dve_border_saturation,
                            border_luma: d.dve_border_luma,
                            border_outer_width: d.dve_border_outer_width,
                            border_inner_width: d.dve_border_inner_width,
                            border_outer_softness: d.dve_border_outer_softness,
                            border_inner_softness: d.dve_border_inner_softness,
                            border_opacity: d.dve_border_opacity,
                            border_bevel_position: d.dve_border_bevel_position,
                            border_bevel_softness: d.dve_border_bevel_softness,
                            light_direction: d.dve_light_direction,
                            light_altitude: d.dve_light_altitude,
                        };
                        localUskChroma[i] = {
                            foreground: d.chroma_foreground,
                            background: d.chroma_background,
                            key_edge: d.chroma_key_edge,
                            spill: d.chroma_spill,
                            flare_suppression: d.chroma_flare_suppression,
                            brightness: d.chroma_brightness,
                            contrast: d.chroma_contrast,
                            saturation: d.chroma_saturation,
                            red: d.chroma_red,
                            green: d.chroma_green,
                            blue: d.chroma_blue,
                        };
                    }
                }
            }
            const localDSKs = (this.state.dsks || []).map(d =>
                d ? { clip: d.clip, gain: d.gain } : null);
            const localStinger = this.state.mes[this.activeMe].transition?.stinger
                ? { clip: this.state.mes[this.activeMe].transition.stinger.clip, gain: this.state.mes[this.activeMe].transition.stinger.gain }
                : null;
            const localDVETx = this.state.mes[this.activeMe].transition?.dve
                ? { clip: this.state.mes[this.activeMe].transition.dve.clip, gain: this.state.mes[this.activeMe].transition.dve.gain }
                : null;
            const localWipe = this.state.mes[this.activeMe].transition
                ? {
                    symmetry: this.state.mes[this.activeMe].transition.wipe_symmetry,
                    softness: this.state.mes[this.activeMe].transition.wipe_softness,
                    width: this.state.mes[this.activeMe].transition.wipe_width,
                }
                : null;

            // Merge incoming state and reassign the top-level ``state``
            // object on the store proxy. The previous per-key merge
            // (``this.state[key] = newState[key]``) didn't reliably wake
            // up Alpine bindings on the FIRST update after page load —
            // dependents reading ``$store.atem.state.mes[activeMe].usk
            // .data[0].chroma_foreground`` would stay on the initial default
            // values until any user interaction triggered a re-render
            // (Issue #15). Diagnostic confirmed the data DID land in the
            // store; the bindings just weren't seeing the change.
            //
            // Reassigning ``state`` itself fires Alpine's outer-proxy
            // ``set`` trap, which forces every dependent effect to
            // re-evaluate against the new sub-tree.
            const merged = { ...this.state };
            Object.keys(newState).forEach(key => {
                if (merged[key] !== undefined) {
                    merged[key] = newState[key];
                } else {
                    // Undeclared top-level key: still dropped (the whitelist
                    // is deliberate), but LOUDLY — see the guard's comment on
                    // window.ATEMControl.reportUndeclaredStateKey.
                    window.ATEMControl.reportUndeclaredStateKey(key);
                }
            });
            this.state = merged;
            // If the connected switcher reports fewer M/Es than the index
            // we're rendering (reconnect to a smaller model mid-session),
            // snap back to M/E 1 before anything reads mes[activeMe].
            if (this.activeMe >= (this.state.mes?.length || 1)) {
                this.activeMe = 0;
            }
            // First real state has now landed — templates that gate on
            // stateReady can render. Stays true for the rest of the
            // session (subsequent updates are reactive in-place).
            this.stateReady = true;

            // Per-field restore for USK luma + pattern + DVE + chroma slider paths.
            // shouldSkip returns true while dragging that path, or while
            // muted and the incoming value hasn't matched the released
            // value yet.
            if (this.state.mes[this.activeMe].usk?.data) {
                for (let i = 0; i < this.state.mes[this.activeMe].usk.data.length; i++) {
                    const d = this.state.mes[this.activeMe].usk.data[i];
                    const local = localUskLuma[i];
                    const localPat = localUskPattern[i];
                    const localDVE = localUskDVE[i];
                    const localChroma = localUskChroma[i];
                    if (!d) continue;
                    if (local) {
                        if (local.clip !== undefined &&
                            guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.luma_clip`, d.luma_clip)) {
                            d.luma_clip = local.clip;
                        }
                        if (local.gain !== undefined &&
                            guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.luma_gain`, d.luma_gain)) {
                            d.luma_gain = local.gain;
                        }
                    }
                    if (localPat) {
                        if (localPat.size !== undefined &&
                            guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.pattern_size`, d.pattern_size)) {
                            d.pattern_size = localPat.size;
                        }
                        if (localPat.symmetry !== undefined &&
                            guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.pattern_symmetry`, d.pattern_symmetry)) {
                            d.pattern_symmetry = localPat.symmetry;
                        }
                        if (localPat.softness !== undefined &&
                            guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.pattern_softness`, d.pattern_softness)) {
                            d.pattern_softness = localPat.softness;
                        }
                    }
                    if (localDVE) {
                        const dveFields = [
                            'rotation', 'border_hue', 'border_saturation', 'border_luma',
                            'border_outer_width', 'border_inner_width',
                            'border_outer_softness', 'border_inner_softness',
                            'border_opacity',
                            'border_bevel_position', 'border_bevel_softness',
                            'light_direction', 'light_altitude',
                        ];
                        for (const f of dveFields) {
                            if (localDVE[f] !== undefined &&
                                guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.dve_${f}`, d[`dve_${f}`])) {
                                d[`dve_${f}`] = localDVE[f];
                            }
                        }
                    }
                    if (localChroma) {
                        const chromaFields = [
                            'foreground', 'background', 'key_edge', 'spill',
                            'flare_suppression', 'brightness', 'contrast',
                            'saturation', 'red', 'green', 'blue',
                        ];
                        for (const f of chromaFields) {
                            if (localChroma[f] !== undefined &&
                                guard.shouldSkip(`atem:me.${this.activeMe}.usk.${i}.chroma_${f}`, d[`chroma_${f}`])) {
                                d[`chroma_${f}`] = localChroma[f];
                            }
                        }
                    }
                }
            }

            // Per-field restore for DSK clip/gain slider paths (per-DSK,
            // mirroring the USK-indexed paths).
            if (this.state.dsks) {
                for (let i = 0; i < this.state.dsks.length; i++) {
                    const d = this.state.dsks[i];
                    const local = localDSKs[i];
                    if (!d || !local) continue;
                    if (local.clip !== undefined &&
                        guard.shouldSkip(`atem:dsk.${i}.clip`, d.clip)) {
                        d.clip = local.clip;
                    }
                    if (local.gain !== undefined &&
                        guard.shouldSkip(`atem:dsk.${i}.gain`, d.gain)) {
                        d.gain = local.gain;
                    }
                }
            }

            // Per-field restore for stinger clip/gain slider paths.
            if (this.state.mes[this.activeMe].transition?.stinger && localStinger) {
                const s = this.state.mes[this.activeMe].transition.stinger;
                if (localStinger.clip !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.stinger.clip`, s.clip)) {
                    s.clip = localStinger.clip;
                }
                if (localStinger.gain !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.stinger.gain`, s.gain)) {
                    s.gain = localStinger.gain;
                }
            }

            // Per-field restore for DVE-tx clip/gain slider paths.
            if (this.state.mes[this.activeMe].transition?.dve && localDVETx) {
                const d = this.state.mes[this.activeMe].transition.dve;
                if (localDVETx.clip !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.dve.clip`, d.clip)) {
                    d.clip = localDVETx.clip;
                }
                if (localDVETx.gain !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.dve.gain`, d.gain)) {
                    d.gain = localDVETx.gain;
                }
            }

            // Per-field restore for wipe slider paths (symmetry / softness / width).
            if (this.state.mes[this.activeMe].transition && localWipe) {
                const t = this.state.mes[this.activeMe].transition;
                if (localWipe.symmetry !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.wipe_symmetry`, t.wipe_symmetry)) {
                    t.wipe_symmetry = localWipe.symmetry;
                }
                if (localWipe.softness !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.wipe_softness`, t.wipe_softness)) {
                    t.wipe_softness = localWipe.softness;
                }
                if (localWipe.width !== undefined &&
                    guard.shouldSkip(`atem:me.${this.activeMe}.transition.wipe_width`, t.wipe_width)) {
                    t.wipe_width = localWipe.width;
                }
            }

            // Update DVE category if style changed
            const currentDVEStyle = this.state.mes[this.activeMe].transition?.dve?.style;
            if (currentDVEStyle !== previousDVEStyle) {
                this.updateDVECategory();
            }
            
            // Update color generator working values when new state arrives.
            // shouldSkip is wait-for-echo: while dragging or muted, the
            // snapshot is dropped UNLESS its value matches what the
            // operator released at (within tolerance) — at which point
            // the registry unmutes and accepts the update. This stops
            // the ATEM's command-queue catch-up from replaying the drag
            // onto the slider after release.
            if (newState.colorGenerators) {
                const gen = this.colorGen.selectedGenerator;
                const currentGen = newState.colorGenerators[gen];
                if (currentGen) {
                    const guard = window.ATEMControl.dragRegistry;
                    if (!guard.shouldSkip(`atem:colorGen.${gen}.hue`, currentGen.hue)) {
                        this.colorGen.hue = currentGen.hue;
                    }
                    if (!guard.shouldSkip(`atem:colorGen.${gen}.saturation`, currentGen.saturation)) {
                        this.colorGen.saturation = currentGen.saturation;
                    }
                    if (!guard.shouldSkip(`atem:colorGen.${gen}.luma`, currentGen.luma)) {
                        this.colorGen.luma = currentGen.luma;
                    }
                }
            }
            
            // Quick-panel per-DSK rate inputs — echo-guarded like the rest.
            const isEditingDskRate = (id) => document.activeElement?.id === id;
            (newState.dsks || []).slice(0, 2).forEach((dsk, i) => {
                if (dsk?.rate_str && !ATEMControl.rateCountdowns['dsk' + i]?.active
                    && !isEditingDskRate('dskRate' + i)) {
                    this.inputs.dskRates[i] = dsk.rate_str;
                }
            });

            // Per-M/E rate inputs mirror the active M/E (shared with
            // setActiveMe, which resyncs them on an M/E switch).
            this._syncMeRateInputs(newMe);

            // Update macros if provided
            if (newState.macros) {
                this.state.macros = newState.macros;
            }
        },

        // Mirror one M/E's rate values into the x-model'd rate inputs.
        // Skips a field while its countdown is running (avoid overwriting
        // the live countdown) or while the operator is mid-typing in it —
        // the focus check stops the polling loop from clobbering a value
        // being edited (x-model two-way binding lets every poll cycle
        // overwrite input.value otherwise; symptom was "Set sometimes
        // works, sometimes doesn't").
        _syncMeRateInputs(me) {
            // "Editing" requires the WINDOW to be focused too: an operator
            // who leaves the caret in a rate field and switches to ASC
            // still has that field as document.activeElement, and the
            // guard would hold their stale text forever while ASC changes
            // the rate underneath (found live 2026-08-22). Background
            // window -> switcher state wins.
            const winFocused = document.hasFocus();
            const isEditing = (id) => winFocused && document.activeElement?.id === id;
            if (me?.transition?.rate && !ATEMControl.rateCountdowns.me.active
                && !isEditing('transitionRate')) {
                this.inputs.transitionRate = me.transition.rate;
            }
            if (me?.ftb?.rate_str && !ATEMControl.rateCountdowns.ftb.active
                && !isEditing('ftbRate') && !isEditing('ftbRate2')) {
                this.inputs.ftbRate = me.ftb.rate_str;
            }
            if (me?.transition) {
                if (me.transition.mix_rate && !ATEMControl.rateCountdowns.mix.active
                    && !isEditing('mixRate')) {
                    this.inputs.mixRate = me.transition.mix_rate;
                }
                if (me.transition.dip_rate && !ATEMControl.rateCountdowns.dip.active
                    && !isEditing('dipRate')) {
                    this.inputs.dipRate = me.transition.dip_rate;
                }
                if (me.transition.wipe_rate && !ATEMControl.rateCountdowns.wipe.active
                    && !isEditing('wipeRate')) {
                    this.inputs.wipeRate = me.transition.wipe_rate;
                }
                if (me.transition.dve_rate && !ATEMControl.rateCountdowns.dve.active
                    && !isEditing('dveRate')) {
                    this.inputs.dveRate = me.transition.dve_rate;
                }
                if (me.transition.stinger_rate && !ATEMControl.rateCountdowns.stinger.active) {
                    this.inputs.stingerRate = me.transition.stinger_rate;
                }

                // Stinger time inputs
                if (me.transition.stinger) {
                    this.inputs.stingerClipDuration = me.transition.stinger.clip_duration_str || '6:00';
                    this.inputs.stingerTriggerPoint = me.transition.stinger.trigger_point_str || '1:09';
                    this.inputs.stingerMixRate = me.transition.stinger.mix_rate_str || '0:05';
                    this.inputs.stingerPreRoll = me.transition.stinger.pre_roll_str || '1:18';
                }
            }

            // Flying-key DVE rate, per keyer. The input ids are
            // usk<k>-<panel>-dve-rate (one per coexisting key-type panel);
            // skip a keyer while any of its panels' rate inputs has focus,
            // same focus-guard idea as the named ids above. Before this
            // sync existed the input was :value-bound to polled state,
            // which rewrote the operator's typing within one poll tick —
            // Set then read the reverted field (found live 2026-08-22).
            (me?.usk?.data || []).forEach((d, k) => {
                if (!d || !d.dve_rate) return;
                const focusId = (winFocused && document.activeElement?.id) || '';
                if (focusId.startsWith('usk' + k + '-') && focusId.endsWith('-dve-rate')) return;
                this.inputs.uskDveRates[k] = ATEMControl.formatFramesToTimeCode(d.dve_rate);
            });
        },

        // Switch the rendered/controlled M/E (Stage 4A). Everything that
        // reads mes[activeMe] re-scopes reactively; sends pick up the new
        // index via the cmd() payload stamp; rate inputs resync here
        // instead of waiting for the next poll.
        setActiveMe(i) {
            if (i === this.activeMe || !this.state.mes?.[i]) return;
            this.activeMe = i;
            this._syncMeRateInputs(this.state.mes[i]);
        },
        
        // Computed properties
        get statusDotClass() {
            return this.connected 
                ? 'w-3 h-3 rounded-full bg-success'
                : 'w-3 h-3 rounded-full bg-error animate-pulse';
        },
        
        get bodyWarningClass() {
            return this.state.mes[this.activeMe].ftb.active 
                ? 'ftb-warning-active ftb-content-offset'
                : '';
        },
        
        get showFTBBanner() {
            return this.state.mes[this.activeMe].ftb.active;
        },
        
        get currentPageMacros() {
            const start = this.macros.currentPage * this.macros.perPage;
            const end = start + this.macros.perPage;
            const macros = [];
            
            for (let i = start; i < end && i < this.macros.total; i++) {
                const macroData = this.state.macros[i];
                macros.push({
                    number: i + 1,
                    name: (macroData?.name?.trim()) || `Macro ${i + 1}`
                });
            }
            return macros;
        },
        
        // Color Generator computed hex value for live preview
        get currentColorHex() {
            return this.hslToHex(this.colorGen.hue, this.colorGen.saturation, this.colorGen.luma);
        },
        
        // HSL to Hex conversion for display
        hslToHex(h, s, l) {
            h = h / 360;
            s = s / 100;
            l = l / 100;
            
            const hue2rgb = (p, q, t) => {
                if (t < 0) t += 1;
                if (t > 1) t -= 1;
                if (t < 1/6) return p + (q - p) * 6 * t;
                if (t < 1/2) return q;
                if (t < 2/3) return p + (q - p) * (2/3 - t) * 6;
                return p;
            };
            
            let r, g, b;
            if (s === 0) {
                r = g = b = l;
            } else {
                const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
                const p = 2 * l - q;
                r = hue2rgb(p, q, h + 1/3);
                g = hue2rgb(p, q, h);
                b = hue2rgb(p, q, h - 1/3);
            }
            
            const toHex = (c) => {
                const hex = Math.round(c * 255).toString(16);
                return hex.length === 1 ? '0' + hex : hex;
            };
            
            return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
        },
        
        // RESTORED: Dynamic button class generators with transition logic
        getSourceButtonClass(type, sourceValue) {
            const currentSource = this.state.mes[this.activeMe][type];
            const isActive = currentSource === sourceValue;
            const inTransition = this.state.mes[this.activeMe].transition.in_transition;
            
            if (type === 'program') {
                return isActive 
                    ? 'btn source-btn bg-red-600 border-red-600 text-white shadow-lg shadow-red-500/40 hover:bg-red-700'
                    : 'btn btn-outline source-btn hover:border-red-300';
            } else {
                // Preview changes color during transition
                if (isActive) {
                    return inTransition
                        ? 'btn source-btn bg-red-600 border-red-600 text-white shadow-lg shadow-red-500/40'
                        : 'btn source-btn bg-green-600 border-green-600 text-white shadow-lg shadow-green-500/40 hover:bg-green-700';
                } else {
                    return inTransition
                        ? 'btn btn-outline source-btn hover:border-red-300'
                        : 'btn btn-outline source-btn hover:border-green-300';
                }
            }
        },
        
        getControlButtonClass(type, index = null) {
            // dsk/dsk_auto take a 0-based DSK index (quick-panel columns
            // and the per-DSK settings accordions both pass one; absent
            // falls back to dsks[0], which the initial state literal
            // guarantees exists before the first snapshot).
            const dsk = this.state.dsks[index ?? 0] || {};
            const buttonStates = {
                usk: this.state.mes[this.activeMe].usk.states[index],
                dsk: dsk.on_air,
                ftb: this.state.mes[this.activeMe].ftb.active,
                dsk_auto: dsk.in_transition || dsk.is_auto_transitioning
            };
            
            const isActive = buttonStates[type];
            const baseClass = isActive 
                ? 'btn control-btn bg-red-600 border-red-600 text-white shadow-lg shadow-red-500/40'
                : 'btn btn-outline control-btn hover:border-red-300';
            
            // Add pulse animation for FTB when active
            if (type === 'ftb' && isActive) {
                return `${baseClass} animate-pulse`;
            }
            
            return baseClass;
        },
        
        getTransitionStyleButtonClass(styleIndex) {
            // Active transition style is a selection, not an on-air state —
            // it takes primary so green stays reserved for preview.
            const isActive = this.state.mes[this.activeMe].transition.style === styleIndex;
            return isActive
                ? 'btn btn-sm btn-primary shadow-lg'
                : 'btn btn-sm btn-outline hover:border-[oklch(var(--p))]';
        },
        
        getTransitionSelectionButtonClass(keyType, keyIndex = null) {
            const selection = this.state.mes[this.activeMe].transition.selection;
            const isSelected = keyType === 'background' 
                ? selection.background 
                : selection[`key${keyIndex + 1}`];
            
            return isSelected
                ? 'btn control-btn text-xs btn-warning shadow-lg'
                : 'btn btn-outline control-btn text-xs hover:border-[oklch(var(--wa))]';
        },

        // Wipe pattern button class - handles type mismatches and uses only ATEM state
        getWipePatternButtonClass(patternIndex) {
            // Convert both values to integers for comparison to handle type mismatches
            const currentPattern = parseInt(this.state.mes[this.activeMe].transition.wipe_pattern) || 0;
            const targetPattern = parseInt(patternIndex);
            const isActive = currentPattern === targetPattern;
            
            return isActive
                ? 'wipe-pattern-button active'
                : 'wipe-pattern-button';
        },

        // UPDATED: Rate input styling with countdown support for all rate types
        getRateInputClass(inputType) {
            const countdown = ATEMControl.rateCountdowns[inputType];
            if (!countdown || !countdown.active) {
                return 'input input-bordered input-sm';
            }
            
            const baseClass = 'input input-sm border-[oklch(var(--in))] bg-[oklch(var(--in)/0.15)] text-[oklch(var(--in))] font-semibold';

            // Check if in ending phase
            const inputElement = document.getElementById(countdown.inputId);
            if (inputElement) {
                const currentValue = inputElement.value;
                const [seconds, frames] = currentValue.split(':').map(Number);
                const totalFrames = (seconds || 0) * ATEMControl.fps() + (frames || 0);

                if (totalFrames <= 10) {
                    return `${baseClass} animate-pulse ring-2 ring-[oklch(var(--wa))]`;
                }
            }
            
            return baseClass;
        },
        
        // Color Generator Methods with throttling for smooth performance
        selectColorGenerator(genIndex) {
            this.colorGen.selectedGenerator = genIndex;
            const gen = this.state.colorGenerators[genIndex];
            if (gen) {
                this.colorGen.hue = gen.hue;
                this.colorGen.saturation = gen.saturation;
                this.colorGen.luma = gen.luma;
            }
        },
        
        // Color generator sliders are realtime — every drag tick must
        // hit program output. RealtimeSlider throttles outbound sends
        // (~25 Hz, leading + trailing) and registers the path in the
        // drag registry so updateState() doesn't snap the slider back
        // mid-drag from a stale polling snapshot.

        _colorGenSendFn(field) {
            const gen = this.colorGen.selectedGenerator;
            if (field === 'hue') {
                return (v) => this.send('set_color_generator_hue', { generator: gen, hue: v });
            }
            if (field === 'saturation') {
                return (v) => this.send('set_color_generator_saturation', { generator: gen, saturation: v });
            }
            if (field === 'luma') {
                return (v) => this.send('set_color_generator_luma', { generator: gen, luma: v });
            }
        },

        startColorGenSliderDrag(field) {
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.${field}`;
            window.ATEMControl.dragRegistry.startDrag(path);
        },

        updateColorGenHue(value) {
            const parsed = parseFloat(value);
            this.colorGen.hue = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.hue`;
            window.ATEMControl.RealtimeSlider.drag(path, parsed, this._colorGenSendFn('hue'));
        },

        updateColorGenSaturation(value) {
            const parsed = parseFloat(value);
            this.colorGen.saturation = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.saturation`;
            window.ATEMControl.RealtimeSlider.drag(path, parsed, this._colorGenSendFn('saturation'));
        },

        updateColorGenLuma(value) {
            const parsed = parseFloat(value);
            this.colorGen.luma = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.luma`;
            window.ATEMControl.RealtimeSlider.drag(path, parsed, this._colorGenSendFn('luma'));
        },

        // Color gen wire scales: hue u16×10 (step 0.1°), sat/luma
        // u16×1000 from 0..1 unit (step 0.1%). Wire grid is finer-or-
        // equal to UI step in all three, so tolerance defaults to the
        // slider's step (0.1). expectedValue is snapped to that grid.

        releaseColorGenHue(value) {
            const parsed = parseFloat(value);
            this.colorGen.hue = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.hue`;
            window.ATEMControl.RealtimeSlider.release(path, parsed, this._colorGenSendFn('hue'), { step: 0.1 });
        },

        releaseColorGenSaturation(value) {
            const parsed = parseFloat(value);
            this.colorGen.saturation = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.saturation`;
            window.ATEMControl.RealtimeSlider.release(path, parsed, this._colorGenSendFn('saturation'), { step: 0.1 });
        },

        releaseColorGenLuma(value) {
            const parsed = parseFloat(value);
            this.colorGen.luma = parsed;
            const path = `atem:colorGen.${this.colorGen.selectedGenerator}.luma`;
            window.ATEMControl.RealtimeSlider.release(path, parsed, this._colorGenSendFn('luma'), { step: 0.1 });
        },
        
        updateColorGenFromHex(hexValue) {
            // Clean input value
            const cleanHex = hexValue.trim();
            
            // Only process complete, valid hex values
            const hexRegex = /^#?([A-Fa-f0-9]{6}|[A-Fa-f0-9]{3})$/;
            if (!hexRegex.test(cleanHex)) {
                // Invalid or incomplete format - don't update ATEM, just return
                return;
            }
            
            // Ensure hex starts with #
            const hex = cleanHex.startsWith('#') ? cleanHex : '#' + cleanHex;
            
            // Convert hex to HSL
            const hsl = this.hexToHsl(hex);
            if (!hsl) return; // Conversion failed
            
            // Update working values immediately (optimistic update)
            this.colorGen.hue = hsl.h;
            this.colorGen.saturation = hsl.s;
            this.colorGen.luma = hsl.l;

            // Send all three through RealtimeSlider.release: flushes
            // immediately and arms the mute window so updateState()
            // doesn't snap the values back from a stale snapshot
            // before our commands echo.
            this.releaseColorGenHue(hsl.h);
            this.releaseColorGenSaturation(hsl.s);
            this.releaseColorGenLuma(hsl.l);
        },

        // Set wipe pattern - ensures integer type
        setWipePattern(patternIndex) {
            this.send('set_wipe_pattern', { pattern: parseInt(patternIndex) });
        },

        // Wipe symmetry / softness / width — RealtimeSlider-driven.
        // Number-input @change and ± buttons use the fire-and-forget
        // setters; the slider uses start/drag/release to throttle the
        // wire and guard against echo replay.
        setWipeSymmetry(symmetry) {
            const value = parseFloat(symmetry);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_symmetry = value;
            this.send('set_wipe_symmetry', { symmetry: value });
        },
        startWipeSymmetryDrag() {
            window.ATEMControl.dragRegistry.startDrag(`atem:me.${this.activeMe}.transition.wipe_symmetry`);
        },
        dragWipeSymmetry(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_symmetry = parsed;
            window.ATEMControl.RealtimeSlider.drag(`atem:me.${this.activeMe}.transition.wipe_symmetry`, parsed, (v) => {
                this.send('set_wipe_symmetry', { symmetry: v });
            });
        },
        releaseWipeSymmetry(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_symmetry = parsed;
            window.ATEMControl.RealtimeSlider.release(`atem:me.${this.activeMe}.transition.wipe_symmetry`, parsed, (v) => {
                this.send('set_wipe_symmetry', { symmetry: v });
            }, { step: 0.1 });
        },

        setWipeSoftness(softness) {
            const value = parseFloat(softness);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_softness = value;
            this.send('set_wipe_softness', { softness: value });
        },
        startWipeSoftnessDrag() {
            window.ATEMControl.dragRegistry.startDrag(`atem:me.${this.activeMe}.transition.wipe_softness`);
        },
        dragWipeSoftness(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_softness = parsed;
            window.ATEMControl.RealtimeSlider.drag(`atem:me.${this.activeMe}.transition.wipe_softness`, parsed, (v) => {
                this.send('set_wipe_softness', { softness: v });
            });
        },
        releaseWipeSoftness(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_softness = parsed;
            window.ATEMControl.RealtimeSlider.release(`atem:me.${this.activeMe}.transition.wipe_softness`, parsed, (v) => {
                this.send('set_wipe_softness', { softness: v });
            }, { step: 0.1 });
        },

        setWipeWidth(width) {
            const value = parseFloat(width);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_width = value;
            this.send('set_wipe_width', { width: value });
        },
        startWipeWidthDrag() {
            window.ATEMControl.dragRegistry.startDrag(`atem:me.${this.activeMe}.transition.wipe_width`);
        },
        dragWipeWidth(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_width = parsed;
            window.ATEMControl.RealtimeSlider.drag(`atem:me.${this.activeMe}.transition.wipe_width`, parsed, (v) => {
                this.send('set_wipe_width', { width: v });
            });
        },
        releaseWipeWidth(value) {
            const parsed = parseFloat(value);
            if (this.state.mes[this.activeMe].transition) this.state.mes[this.activeMe].transition.wipe_width = parsed;
            window.ATEMControl.RealtimeSlider.release(`atem:me.${this.activeMe}.transition.wipe_width`, parsed, (v) => {
                this.send('set_wipe_width', { width: v });
            }, { step: 0.1 });
        },
        
        // Validate hex input for visual feedback - more lenient during typing
        isValidHex(hexValue) {
            if (!hexValue || hexValue.trim() === '') return true; // Empty is valid
            const cleanHex = hexValue.trim();
            
            // Allow common typing patterns
            if (cleanHex === '#') return true;
            if (/^#?[A-Fa-f0-9]*$/.test(cleanHex) && cleanHex.replace('#', '').length <= 6) {
                return true; // Valid characters, acceptable length
            }
            
            return false; // Contains invalid characters
        },
        
        // Convert hex to HSL for ATEM
        hexToHsl(hex) {
            try {
                // Remove # if present and expand shorthand
                hex = hex.replace('#', '').toUpperCase();
                
                // Expand 3-character hex to 6-character (e.g., F00 -> FF0000)
                if (hex.length === 3) {
                    hex = hex.split('').map(char => char + char).join('');
                }
                
                // Validate length
                if (hex.length !== 6) return null;
                
                // Convert to RGB
                const r = parseInt(hex.substr(0, 2), 16) / 255;
                const g = parseInt(hex.substr(2, 2), 16) / 255;
                const b = parseInt(hex.substr(4, 2), 16) / 255;
                
                // Find min and max values
                const max = Math.max(r, g, b);
                const min = Math.min(r, g, b);
                const diff = max - min;
                
                // Calculate luminance (0-100%)
                const l = ((max + min) / 2) * 100;
                
                // Calculate saturation (0-100%)
                let s = 0;
                if (diff !== 0) {
                    s = l < 50 ? (diff / (max + min)) * 100 : (diff / (2 - max - min)) * 100;
                }
                
                // Calculate hue (0-359.9 degrees)
                let h = 0;
                if (diff !== 0) {
                    if (max === r) {
                        h = ((g - b) / diff + (g < b ? 6 : 0)) * 60;
                    } else if (max === g) {
                        h = ((b - r) / diff + 2) * 60;
                    } else {
                        h = ((r - g) / diff + 4) * 60;
                    }
                }
                
                return {
                    h: Math.round(h * 10) / 10, // Round to 1 decimal place
                    s: Math.round(s * 10) / 10,
                    l: Math.round(l * 10) / 10
                };
                
            } catch (e) {
                console.warn('Error converting hex to HSL:', e);
                return null;
            }
        },
        
        // Command methods
        async quickConnect() {
            const raw = this.inputs.quickConnectIP.trim();
            if (!raw) return;

            // Name-or-IP: a literal IP connects as typed; anything else
            // resolves through the equipment inventory — exact name match
            // first, then a unique filter match. None or ambiguous: keep
            // the text and show the dropdown so the operator picks.
            let ipAddress = raw;
            if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(raw)) {
                const matches = this._atemEquipmentMatches(raw);
                const exact = matches.find(e => e.name.toLowerCase() === raw.toLowerCase());
                // A unique NAME hit wins over IP/location coincidences
                const nameHits = matches.filter(e => this._nameMatchesAll(e, this._queryTokens(raw)));
                const target = exact
                    || (matches.length === 1 ? matches[0] : null)
                    || (nameHits.length === 1 ? nameHits[0] : null);
                if (!target) {
                    this.quickConnectOpen = true;
                    return;
                }
                ipAddress = target.ip;
            }

            this.inputs.quickConnectIP = ''; // Clear input immediately
            this.quickConnectOpen = false;

            // Delegate to the single WebSocket connect flow in
            // ATEMControl.connect (this method used to carry its own
            // duplicated ~165-line WS flow). userInitiated keeps a failed
            // attempt on this page (inline error instead of the
            // redirect-to-Connect flow); friendlyErrors maps raw server
            // failure messages to the operator-facing hints this input has
            // always shown; updateUrl rewrites ?ip=/&name= on success.
            await ATEMControl.connect(ipAddress, {
                userInitiated: true,
                updateUrl: true,
                friendlyErrors: true,
            });
        },

        // Dropdown interaction for the quick-connect input (wired in
        // control/_header.html).
        quickConnectQueryChanged() {
            this.quickConnectOpen = true;
            this.quickConnectHighlight = 0;
        },

        quickConnectMove(delta) {
            const n = this.quickConnectMatches.length;
            if (!n) return;
            this.quickConnectOpen = true;
            this.quickConnectHighlight = (this.quickConnectHighlight + delta + n) % n;
        },

        quickConnectEnter() {
            const raw = this.inputs.quickConnectIP.trim();
            const matches = this.quickConnectMatches;
            // Highlighted suggestion wins unless the operator typed a
            // literal IP; quickConnect() handles the rest.
            if (this.quickConnectOpen && matches.length &&
                !/^\d{1,3}(\.\d{1,3}){3}$/.test(raw)) {
                this.selectQuickConnect(matches[Math.min(this.quickConnectHighlight, matches.length - 1)]);
            } else {
                this.quickConnect();
            }
        },

        selectQuickConnect(match) {
            this.inputs.quickConnectIP = '';
            this.quickConnectOpen = false;
            this.quickConnectHighlight = 0;
            ATEMControl.connect(match.ip, {
                userInitiated: true,
                updateUrl: true,
                friendlyErrors: true,
            });
        },
        
        disconnect() {
            // Cancel any in-flight color generator slider sends
            const gen = this.colorGen.selectedGenerator;
            ['hue', 'saturation', 'luma'].forEach(field => {
                window.ATEMControl.RealtimeSlider.cancel(`atem:colorGen.${gen}.${field}`);
            });

            // Mark this as user-initiated so the connection_status
            // handler (which fires when the server echoes the
            // disconnect) doesn't stash a "lost connection" message
            // on the Connect page.
            this._userInitiatedDisconnect = true;
            try { sessionStorage.removeItem('atem_disconnect_reason'); } catch (e) {}
            ATEMControl.cmd('disconnect');
            localStorage.removeItem('atem_ip');
            window.location.href = '/atem/';
        },
        
        // Macro with animation
        executeMacro(macroNumber) {
            this.send('execute_macro', { macro_number: macroNumber });
            this.triggerMacroAnimation(macroNumber);
        },
        
        triggerMacroAnimation(macroNumber) {
            const btn = document.querySelector(`[data-macro="${macroNumber}"]`);
            if (btn) {
                btn.classList.add('scale-110', 'shadow-lg', 'shadow-white/80');
                
                setTimeout(() => {
                    btn.classList.add('scale-115', 'shadow-xl', 'shadow-white/60');
                }, 50);
                
                setTimeout(() => {
                    btn.classList.remove('scale-110', 'scale-115', 'shadow-lg', 'shadow-xl', 'shadow-white/80', 'shadow-white/60');
                }, 250);
            }
        },
        
        // Generic command sender
        send(command, params = {}) {
            ATEMControl.cmd(command, params);
        },

        // Audio meter subscription — tab-gated. The Fairlight meter stream
        // is ~375 msg/s sustained on a 14-strip mixer, which saturates the
        // WebSocket write buffer and causes the atem_state lag tracked as
        // Issue #13. We only subscribe while the operator has the audio
        // panel open AND the browser tab is visible. The template wires
        // these via x-effect on audioPanelOpen and visibilitychange.
        audioMetersSubscribed: false,
        subscribeAudioMeters() {
            if (this.audioMetersSubscribed) return;
            if (!this.connected) return;
            const ws = ATEMControl.ws;
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            this.send('subscribe_audio_meters');
            this.audioMetersSubscribed = true;
        },
        unsubscribeAudioMeters() {
            if (!this.audioMetersSubscribed) return;
            // Try to notify the server even if the socket is mid-close;
            // if it fails the server-side refcount drops on disconnect.
            const ws = ATEMControl.ws;
            if (ws && ws.readyState === WebSocket.OPEN) {
                this.send('unsubscribe_audio_meters');
            }
            this.audioMetersSubscribed = false;
        },
    });
});
