/* atem_control_page.js — the control page's root Alpine component.
 *
 * Extracted verbatim from the inline ``x-data`` attribute on the control
 * page's root <div> (Stage 1e item 2): same keys, same methods, same
 * logic. The root div now reads ``x-data="atemControlPage"``.
 *
 * Registration happens on ``alpine:init`` so the component exists before
 * Alpine walks the DOM. Load order is load-bearing: this file MUST
 * execute before the Alpine CDN script. It does — the tag is placed
 * above the Alpine tag in control.html AND is non-deferred while the
 * Alpine CDN tag is ``defer``red.
 *
 * NOTE: several comments in the body below still describe the old
 * inline-attribute constraints (single-quote-only strings, "the whole
 * x-data block is double-quoted"). Those constraints no longer bind in
 * a static file; the text moved unchanged because this extraction is a
 * verbatim relocation.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('atemControlPage', () => ({
    settingsOpen: false,
    mediaPoolOpen: false,
    audioPanelOpen: false,
    /* { mode: 'eq' | 'dyn', stripId, source, channel }. stripId is
       '_master' for the master rail. source/channel are echoed back
       in the dispatch payload (master uses source=0 channel=-1). */
    audioModal: null,
    /* Local override for the EQ make-up gain while the operator is
       dragging the fader. Set by AudioFader.onPreview on every drag tick;
       cleared on release. The visualizer reads via audioModalSubjectForViz
       so the curve tracks the gesture without round-tripping every drag
       sample through ATEM (one CFSP per release, not 20+ per second). */
    eqGainPreview: null,
    /* daisyUI dropdowns are focus-driven, so a second click on the
       trigger just re-focuses it and the menu stays open. When the
       dropdown already has focus (menu open), swallow the mousedown
       before it re-focuses and blur — the menu closes. */
    dropdownToggle(event) {
        const dd = event.currentTarget.closest('.dropdown');
        if (dd && dd.matches(':focus-within')) {
            event.preventDefault();
            document.activeElement.blur();
        }
    },
    audioModalSubject() {
        if (!this.audioModal) return null;
        const audio = this.$store.atem.state?.audio;
        if (!audio) return null;
        if (this.audioModal.stripId === '_master') return audio.master ?? null;
        return audio.strips?.find(s => s.strip_id === this.audioModal.stripId) ?? null;
    },
    /* Same subject the rest of the modal reads, but with eq_gain_db
       overridden by the live drag preview when one is active. The EQ
       curve, the dB readout under the make-up fader, and any other
       visualization that wants to follow the gesture should call this
       instead of audioModalSubject(). */
    audioModalSubjectForViz() {
        let s = this.audioModalSubject();
        if (!s) return s;
        if (this.eqGainPreview !== null) {
            s = { ...s, eq_gain_db: this.eqGainPreview };
        }
        // Live drag preview for the band currently being dragged on the
        // visualizer. Without this, the curve and marker would stay
        // pinned to server state until the CFSP/CEBP echo arrives, which
        // makes the drag feel laggy. The preview is cleared on
        // pointerup; subsequent server echoes paint the final position.
        if (this.eqBandDrag && Array.isArray(s.eq_bands)) {
            const drag = this.eqBandDrag;
            s = { ...s, eq_bands: s.eq_bands.map(b => (
                b.index === drag.bandIdx
                    ? { ...b, frequency: drag.frequency, gain_db: drag.gain_db }
                    : b
            )) };
        }
        return s;
    },
    audioModalTitle() {
        const subj = this.audioModalSubject();
        const name = this.audioModal?.stripId === '_master'
            ? 'Master'
            : (subj?.short_name || subj?.name || '?');
        const mode = this.audioModal?.mode === 'eq' ? 'Equalizer' : 'Dynamics';
        return name + ' — ' + mode;
    },
    /* Filter the bitfield of allowed shapes per band (the ATEM exposes
       which filter types are valid for each band; shelves and pass
       filters are usually only on the edge bands). */
    eqShapesForBand(band) {
        const SHAPES = [
            { name: 'LowShelf',  bit: 0x01, label: 'Low Shelf' },
            { name: 'LowPass',   bit: 0x02, label: 'Low Pass' },
            { name: 'BandPass',  bit: 0x04, label: 'Bell' },
            { name: 'Notch',     bit: 0x08, label: 'Notch' },
            { name: 'HighPass',  bit: 0x10, label: 'High Pass' },
            { name: 'HighShelf', bit: 0x20, label: 'High Shelf' },
        ];
        const allowed = (band && band.possible_filters) || 0xFF;
        return SHAPES.filter(s => (allowed & s.bit) !== 0);
    },
    setEqEnable(enabled) {
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') {
            ATEMControl.cmd('set_audio_master_eq_enable', { enabled });
        } else {
            ATEMControl.cmd('set_audio_strip_eq_enable', {
                source: this.audioModal.source,
                channel: this.audioModal.channel,
                enabled,
            });
        }
    },
    setBandProp(bandIdx, props) {
        // props is one or more of: enabled, shape, frequency, gain_db, q, range
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') {
            // F4: master-bus EQ bands go to CMBP, NOT a per-strip CEBP at
            // source 0 (a non-existent strip) — matches setEqEnable/setMakeupGain.
            ATEMControl.cmd('set_audio_master_eq_band', { band: bandIdx, ...props });
        } else {
            ATEMControl.cmd('set_audio_eq_band', {
                source: this.audioModal.source,
                channel: this.audioModal.channel,
                band: bandIdx,
                ...props,
            });
        }
    },
    setMakeupGain(gain_db) {
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') {
            ATEMControl.cmd('set_audio_master_dynamics_makeup', { gain_db });
        } else {
            ATEMControl.cmd('set_audio_strip_dynamics_makeup', {
                source: this.audioModal.source,
                channel: this.audioModal.channel,
                gain_db,
            });
        }
    },

    /* -------------------- Dynamics block helpers -------------------- */

    /* Read one of the three dynamics blocks (compressor / limiter /
       expander) off the strip currently in the modal. Returns the
       per-block dict shape from pyatem.messages.fairlight (keys like threshold_db,
       attack_ms, ratio, range_db, ...) or an empty object when the
       AICP/AILP/AIXP echo hasn't arrived yet. */
    dynamicsBlock(name) {
        const s = this.audioModalSubject();
        return (s && s[name]) || {};
    },

    /* Send one parameter change. ``name`` is 'compressor' / 'limiter' /
       'expander', ``param`` is one of the wire-side keys (threshold_db,
       ratio, range_db, attack_ms, hold_ms, release_ms, enabled). The
       dispatch-side handler in commands.py uses set_audio_<block>. */
    setDynamicsParam(name, param, value) {
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') return;  // master has only makeup
        const verb = 'set_audio_' + name;
        const data = {
            source: this.audioModal.source,
            channel: this.audioModal.channel,
        };
        data[param] = value;
        ATEMControl.cmd(verb, data);
    },

    /* Send multiple dynamics-block parameters in one CIXP/CICP/CILP
       wire packet. Use this for compound state changes — like the
       Expander tab click which needs to set both ``enabled`` and
       ``mode`` atomically. Sending them as two separate packets
       produces a momentary flicker (ATEM processes the first, echoes
       state, frontend updates, then the second packet lands and
       echoes again). One packet = one ATEM state update = no flicker. */
    setDynamics(name, params) {
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') return;
        ATEMControl.cmd('set_audio_' + name, {
            source: this.audioModal.source,
            channel: this.audioModal.channel,
            ...params,
        });
    },

    /* Format a numeric value for compact display; gracefully handles
       missing values during the handshake gap. */
    fmtDyn(value, suffix, fallback = '—') {
        if (value === undefined || value === null || value === '') return fallback;
        const n = Number(value);
        if (!Number.isFinite(n)) return fallback;
        return n.toFixed(2).replace(/\.?0+$/, '') + (suffix || '');
    },

    /* -------------------- Dynamics transfer-curve helpers --------------------
       SVG viewBox is 200x200. X = input level dB (-90 left .. 0 right);
       Y = output level dB (0 top .. -90 bottom). The 1:1 reference line
       runs from (0,200) to (200,0). The curve is a piecewise function
       composed of expander/gate (below their threshold), compressor
       (above its threshold), and limiter (hard cap above its threshold).
       The make-up offset is **not** folded in — Software Control shows
       the dynamics processing alone on the transfer curve and surfaces
       make-up separately on the fader readout. Output is clamped to the
       viewbox so very negative values just peg at the bottom. */
    dynamicsDbToX(db) {
        const x = ((db + 90) / 90) * 200;
        return Math.max(0, Math.min(200, x));
    },
    dynamicsDbToY(db) {
        // Clamp the top (output never exceeds 0 dB through any block in
        // this chain) but let the bottom run off the viewbox so very-low
        // outputs disappear off the floor instead of forming a visible
        // horizontal plateau at y=200. SVG clipping handles the rest.
        const y = (-db / 90) * 200;
        return Math.max(0, y);
    },
    /* Pure transfer function: input dB → output dB given a subject's
       expander / compressor / limiter blocks. Shared by the modal's
       full-size path and the per-strip mini. Make-up gain is NOT
       folded in (Software Control surfaces it on the fader, not the
       transfer curve). Returns inDb when the subject has no dynamics
       blocks (master rail) or all blocks are bypassed. */
    dynamicsTransferOutDb(inDb, subject) {
        const exp = (subject && subject.expander) || {};
        const comp = (subject && subject.compressor) || {};
        const lim = (subject && subject.limiter) || {};
        let outDb = inDb;
        if (exp.enabled && exp.threshold_db !== undefined) {
            const thr = exp.threshold_db;
            const range = exp.range_db ?? 0;
            if (inDb < thr) {
                if (exp.mode === 'gate') {
                    outDb -= range;
                } else {
                    // Standard downward expander: below threshold, the
                    // slope is ``ratio:1`` (e.g. ratio=3 gives a 3:1
                    // slope downward). Total reduction = (thr - in) *
                    // (ratio - 1), capped at the strip's range setting.
                    const ratio = exp.ratio ?? 2;
                    let red = (thr - inDb) * (ratio - 1);
                    if (red > range) red = range;
                    outDb -= red;
                }
            }
        }
        if (comp.enabled && comp.threshold_db !== undefined) {
            const thr = comp.threshold_db;
            const ratio = comp.ratio ?? 1.2;
            if (outDb > thr) {
                outDb = thr + (outDb - thr) / ratio;
            }
        }
        if (lim.enabled && lim.threshold_db !== undefined) {
            const thr = lim.threshold_db;
            if (outDb > thr) outDb = thr;
        }
        return outDb;
    },
    dynamicsAnyEnabled(subject) {
        if (!subject) return false;
        const exp = subject.expander || {};
        const comp = subject.compressor || {};
        const lim = subject.limiter || {};
        return Boolean(exp.enabled || comp.enabled || lim.enabled);
    },
    dynamicsTransferPath() {
        const subject = this.audioModalSubject();
        // No active processing → no yellow curve. The grid + dashed
        // 1:1 reference diagonal already shows unity, so a coincident
        // yellow line on top would just be noise.
        if (!this.dynamicsAnyEnabled(subject)) return '';
        const pts = [];
        for (let i = 0; i <= 180; i++) {
            const inDb = -90 + i * 0.5;
            const outDb = this.dynamicsTransferOutDb(inDb, subject);
            const x = this.dynamicsDbToX(inDb);
            const y = this.dynamicsDbToY(outDb);
            pts.push((i === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1));
        }
        return pts.join(' ');
    },

    /* Mini dynamics transfer curve for the per-strip preview tile.
       60 x 32 viewBox; same piecewise transfer as the modal but
       projected into a small box. Mirrors ``eqMiniPath``: always
       renders, caller switches stroke color via the enabled state.
       When all blocks are bypassed the curve is a 1:1 diagonal — the
       reference position the modal also shows when nothing is on. */
    dynamicsMiniPath(subject) {
        if (!subject) return '';
        const W = 60, H = 32;
        const DB_MIN = -90, DB_MAX = 0;
        const dbToX = (db) => ((db - DB_MIN) / (DB_MAX - DB_MIN)) * W;
        const dbToY = (db) => Math.max(0, (-db / -DB_MIN) * H);
        const N = 60;
        const parts = [];
        for (let i = 0; i <= N; i++) {
            const inDb = DB_MIN + (i / N) * (DB_MAX - DB_MIN);
            const outDb = this.dynamicsTransferOutDb(inDb, subject);
            const x = dbToX(inDb).toFixed(1);
            const y = dbToY(outDb).toFixed(1);
            parts.push(i === 0 ? `M ${x},${y}` : `L ${x},${y}`);
        }
        return parts.join(' ');
    },

    setEqGain(gain_db) {
        // Post-EQ make-up gain (the right-side fader in the EQ viz). Range
        // -20..+20 dB. Master uses CFMP, strips use CFSP.
        if (!this.audioModal) return;
        if (this.audioModal.stripId === '_master') {
            ATEMControl.cmd('set_audio_master_eq_gain', { gain_db });
        } else {
            ATEMControl.cmd('set_audio_strip_eq_gain', {
                source: this.audioModal.source,
                channel: this.audioModal.channel,
                gain_db,
            });
        }
    },

    /* -------------------- EQ visualization helpers -------------------- */
    /* SVG viewBox is fixed (1000x280); CSS scales it to fit the modal
       width via preserveAspectRatio. X axis = log10(frequency), Y axis
       = gain dB. The cumulative response sums each enabled band, plus
       the post-EQ make-up gain. (Avoid double-quotes in this comment —
       the whole x-data block is itself a double-quoted attribute, so
       a stray closing quote here breaks the entire HTML attribute and
       the browser renders the JS source as page text.) */
    EQ_VIZ: {
        W: 1000, H: 280,
        PAD_L: 40, PAD_R: 40, PAD_T: 12, PAD_B: 28,
        F_MIN: 20, F_MAX: 22000,
        G_MIN: -20, G_MAX: 20,
    },
    eqInnerW() { return this.EQ_VIZ.W - this.EQ_VIZ.PAD_L - this.EQ_VIZ.PAD_R; },
    eqInnerH() { return this.EQ_VIZ.H - this.EQ_VIZ.PAD_T - this.EQ_VIZ.PAD_B; },
    eqFreqToX(f) {
        const lg = Math.log10;
        const t = (lg(f) - lg(this.EQ_VIZ.F_MIN))
                  / (lg(this.EQ_VIZ.F_MAX) - lg(this.EQ_VIZ.F_MIN));
        return this.EQ_VIZ.PAD_L + t * this.eqInnerW();
    },
    eqGainToY(g) {
        const c = Math.max(this.EQ_VIZ.G_MIN, Math.min(this.EQ_VIZ.G_MAX, g));
        return this.EQ_VIZ.H - this.EQ_VIZ.PAD_B
               - (c - this.EQ_VIZ.G_MIN) / (this.EQ_VIZ.G_MAX - this.EQ_VIZ.G_MIN)
                 * this.eqInnerH();
    },
    /* Inverse mappings — pointer in viewBox coordinates → freq/gain.
       Used by the band-marker drag handler. Clamped at the axis limits
       so dragging past the edge just pegs at min/max. */
    eqXToFreq(x) {
        const lg = Math.log10;
        const t = (x - this.EQ_VIZ.PAD_L) / this.eqInnerW();
        const tc = Math.max(0, Math.min(1, t));
        const lgF = lg(this.EQ_VIZ.F_MIN)
                    + tc * (lg(this.EQ_VIZ.F_MAX) - lg(this.EQ_VIZ.F_MIN));
        return Math.pow(10, lgF);
    },
    eqYToGain(y) {
        const tFromBottom = (this.EQ_VIZ.H - this.EQ_VIZ.PAD_B - y) / this.eqInnerH();
        const tc = Math.max(0, Math.min(1, tFromBottom));
        return this.EQ_VIZ.G_MIN
               + tc * (this.EQ_VIZ.G_MAX - this.EQ_VIZ.G_MIN);
    },
    eqGridFreqs: [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000],
    eqGridGains: [-20, -10, 0, 10, 20],
    eqFreqLabel(f) { return f >= 1000 ? (f / 1000) + 'K' : String(f); },

    /* Per-band frequency magnitude response — standard RBJ biquad
       transfer functions (the formulas every DAW EQ uses), evaluated at
       freq via |H(e^jw)|^2 = |B(e^jw)|^2 / |A(e^jw)|^2. Sample rate is
       fixed at 48 kHz to match Fairlights internal rate; the visualizer
       only needs the SHAPE so an exact match to ATEMs internal rate is
       not required. Summed across bands by the caller. */
    eqBandResponseAt(band, freq) {
        if (!band || !band.enabled) return 0;
        const f0 = band.frequency || 1000;
        const g = band.gain_db || 0;
        // band.q is the user-facing Q control. ATEMs Software Control
        // only exposes Q for Bell (BandPass); for shelves, LP/HP, and
        // Notch the Q control is hidden because ATEM uses a fixed
        // Butterworth-style slope internally (S=1, equivalent to
        // Q=1/sqrt(2)). Feeding the stored band.q into the biquad for
        // those filters produces visible resonance peaks that ATEM
        // doesnt show. Use Q=0.707 for non-Bell filters to match.
        const isBell = band.filter === 'BandPass';
        const q = isBell ? Math.max(0.1, band.q || 1.0) : 0.7071067811865476;
        const FS = 48000;
        const w0 = 2 * Math.PI * f0 / FS;
        const cosw0 = Math.cos(w0);
        const sinw0 = Math.sin(w0);
        const A = Math.pow(10, g / 40);  // peaking/shelf amplitude
        const alpha = sinw0 / (2 * q);

        let b0, b1, b2, a0, a1, a2;
        switch (band.filter) {
            case 'BandPass':  // Bell / peaking EQ
                b0 = 1 + alpha * A;
                b1 = -2 * cosw0;
                b2 = 1 - alpha * A;
                a0 = 1 + alpha / A;
                a1 = -2 * cosw0;
                a2 = 1 - alpha / A;
                break;
            case 'LowShelf': {
                const beta = Math.sqrt(A) / q;
                b0 = A * ((A + 1) - (A - 1) * cosw0 + beta * sinw0);
                b1 = 2 * A * ((A - 1) - (A + 1) * cosw0);
                b2 = A * ((A + 1) - (A - 1) * cosw0 - beta * sinw0);
                a0 = (A + 1) + (A - 1) * cosw0 + beta * sinw0;
                a1 = -2 * ((A - 1) + (A + 1) * cosw0);
                a2 = (A + 1) + (A - 1) * cosw0 - beta * sinw0;
                break;
            }
            case 'HighShelf': {
                const beta = Math.sqrt(A) / q;
                b0 = A * ((A + 1) + (A - 1) * cosw0 + beta * sinw0);
                b1 = -2 * A * ((A - 1) + (A + 1) * cosw0);
                b2 = A * ((A + 1) + (A - 1) * cosw0 - beta * sinw0);
                a0 = (A + 1) - (A - 1) * cosw0 + beta * sinw0;
                a1 = 2 * ((A - 1) - (A + 1) * cosw0);
                a2 = (A + 1) - (A - 1) * cosw0 - beta * sinw0;
                break;
            }
            case 'LowPass':
                b0 = (1 - cosw0) / 2;
                b1 = 1 - cosw0;
                b2 = (1 - cosw0) / 2;
                a0 = 1 + alpha;
                a1 = -2 * cosw0;
                a2 = 1 - alpha;
                break;
            case 'HighPass':
                b0 = (1 + cosw0) / 2;
                b1 = -(1 + cosw0);
                b2 = (1 + cosw0) / 2;
                a0 = 1 + alpha;
                a1 = -2 * cosw0;
                a2 = 1 - alpha;
                break;
            case 'Notch':
                b0 = 1;
                b1 = -2 * cosw0;
                b2 = 1;
                a0 = 1 + alpha;
                a1 = -2 * cosw0;
                a2 = 1 - alpha;
                break;
            default:
                return 0;
        }

        // Normalize to a0 = 1
        const B0 = b0 / a0, B1 = b1 / a0, B2 = b2 / a0;
        const A1 = a1 / a0, A2 = a2 / a0;

        // |H(e^jw)|^2, computed via real/imag of B(e^jw) and A(e^jw).
        // The direct form (real^2 + imag^2) is numerically more stable
        // near DC than the expanded sum-of-squares-and-cosines form.
        const w = 2 * Math.PI * freq / FS;
        const cosw = Math.cos(w);
        const sinw = Math.sin(w);
        const cos2w = Math.cos(2 * w);
        const sin2w = Math.sin(2 * w);

        const numR = B0 + B1 * cosw + B2 * cos2w;
        const numI = -B1 * sinw - B2 * sin2w;
        const denR = 1 + A1 * cosw + A2 * cos2w;
        const denI = -A1 * sinw - A2 * sin2w;

        const numSq = numR * numR + numI * numI;
        const denSq = denR * denR + denI * denI;
        if (denSq <= 0 || numSq <= 0) return -120;
        return 10 * Math.log10(numSq / denSq);
    },
    eqResponsePath(subject) {
        if (!subject || !subject.eq_bands) return '';
        // Always render the curve shape, even when bypassed — operators
        // need to see what the EQ is set to so they can decide whether
        // to re-enable it. The bypassed visual cue is the opacity-50
        // class on the SVG itself, mirroring ATEM Software Control.
        const masterG = subject.eq_gain_db || 0;
        const bands = subject.eq_bands;
        const N = 200;
        const parts = [];
        for (let i = 0; i <= N; i++) {
            const t = i / N;
            const freq = this.EQ_VIZ.F_MIN
                         * Math.pow(this.EQ_VIZ.F_MAX / this.EQ_VIZ.F_MIN, t);
            let total = masterG;
            for (const band of bands) total += this.eqBandResponseAt(band, freq);
            const x = this.eqFreqToX(freq).toFixed(1);
            const y = this.eqGainToY(total).toFixed(1);
            parts.push(i === 0 ? `M ${x},${y}` : `L ${x},${y}`);
        }
        return parts.join(' ');
    },
    eqMarkerPos(band) {
        return {
            x: this.eqFreqToX(band.frequency || 1000),
            y: this.eqGainToY(band.gain_db || 0),
        };
    },

    /* Mini EQ curve for the per-strip preview tile on the audio panel.
       60 x 32 viewBox; samples 60 log-spaced frequencies and the same
       biquad transfer functions used in the full visualizer.
       Importantly does NOT honor subject.eq_enable — the curve renders
       regardless and the caller switches stroke color based on enable
       state. Mirrors ATEM Software Controls behaviour: when EQ is
       bypassed, the curve still shows but in grey. */
    eqMiniPath(subject) {
        if (!subject || !Array.isArray(subject.eq_bands)) return '';
        const W = 60, H = 32;
        const F_MIN = 20, F_MAX = 22000;
        const G_MIN = -20, G_MAX = 20;
        const N = 60;
        const lg = Math.log10;
        const lgMin = lg(F_MIN);
        const lgRng = lg(F_MAX) - lgMin;
        const masterG = subject.eq_gain_db || 0;
        const parts = [];
        for (let i = 0; i <= N; i++) {
            const t = i / N;
            const freq = Math.pow(10, lgMin + t * lgRng);
            let total = masterG;
            for (const band of subject.eq_bands) {
                total += this.eqBandResponseAt(band, freq);
            }
            const x = (t * W).toFixed(1);
            const c = Math.max(G_MIN, Math.min(G_MAX, total));
            const y = (H - (c - G_MIN) / (G_MAX - G_MIN) * H).toFixed(1);
            parts.push(i === 0 ? `M ${x},${y}` : `L ${x},${y}`);
        }
        return parts.join(' ');
    },

    /* Band-marker drag state. While non-null, the drag preview shows in
       audioModalSubjectForViz() so the curve and marker follow the
       gesture in realtime. Cleared on pointerup.
       Shape: { bandIdx, frequency, gain_db, throttle: { last, pending, timer } } */
    eqBandDrag: null,

    /* Pointer (clientX/Y) → SVG viewBox coordinates. The SVG keeps a
       fixed 1000x280 viewBox; CSS scales it to the modal's width.
       getBoundingClientRect gives us the pixel rect; map proportionally. */
    eqSvgPoint(svgEl, ev) {
        const rect = svgEl.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return null;
        return {
            x: (ev.clientX - rect.left) / rect.width * this.EQ_VIZ.W,
            y: (ev.clientY - rect.top) / rect.height * this.EQ_VIZ.H,
        };
    },

    /* Hit-test: find the nearest band marker within HIT_RADIUS viewBox
       units of (x, y). Returns the band or null. Disabled bands ARE
       grabbable (matches ATEM Software Control) — operators commonly
       set up frequency/gain on a parked band before re-enabling it. */
    eqHitTestBand(x, y) {
        const subject = this.audioModalSubject();
        if (!subject || !Array.isArray(subject.eq_bands)) return null;
        const HIT_RADIUS = 22;
        let best = null;
        let bestDist = HIT_RADIUS;
        for (const band of subject.eq_bands) {
            const pos = this.eqMarkerPos(band);
            const dx = pos.x - x;
            const dy = pos.y - y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist <= bestDist) {
                bestDist = dist;
                best = band;
            }
        }
        return best;
    },

    /* Whether the band's filter shape uses the gain axis. LowPass /
       HighPass / Notch are frequency-only, so vertical drag is ignored
       for those — matches ATEM Software Control. */
    _eqBandUsesGain(band) {
        return ['LowShelf', 'BandPass', 'HighShelf'].includes(band.filter);
    },

    eqStartBandDrag(ev) {
        // No eq_enable gate — ATEM Software Control lets operators
        // park band positions on a bypassed EQ and re-enable later.
        const svgEl = ev.currentTarget;
        const pt = this.eqSvgPoint(svgEl, ev);
        if (!pt) return;
        const band = this.eqHitTestBand(pt.x, pt.y);
        if (!band) return;
        ev.preventDefault();
        try { svgEl.setPointerCapture(ev.pointerId); } catch (e) {}
        this.eqBandDrag = {
            bandIdx: band.index,
            frequency: band.frequency,
            gain_db: band.gain_db,
            usesGain: this._eqBandUsesGain(band),
            throttle: { lastSentAt: 0, pendingProps: null, timer: null },
        };
    },

    eqDragBand(ev) {
        if (!this.eqBandDrag) return;
        const svgEl = ev.currentTarget;
        const pt = this.eqSvgPoint(svgEl, ev);
        if (!pt) return;
        ev.preventDefault();

        const drag = this.eqBandDrag;
        const newFreq = Math.round(this.eqXToFreq(pt.x));
        // Clamp to ATEM Fairlight EQ range (matches the number-input bounds
        // on each band panel below the visualizer).
        const freq = Math.max(20, Math.min(22000, newFreq));
        const gain = drag.usesGain
            ? Math.max(-20, Math.min(20, this.eqYToGain(pt.y)))
            : drag.gain_db;
        // Round gain to 1 decimal — the band panel's number input uses
        // step=0.1 and the ATEM stores at finer precision but displays
        // at this granularity. Keeping the drag value to the same grid
        // makes the wait-for-echo math work later if we add it.
        // (NO double-quotes in this comment: the entire x-data block is
        //  itself a double-quoted HTML attribute, so a stray ASCII
        //  quote-mark here would close the attribute and dump the JS
        //  source onto the page as text.)
        const gainRounded = Math.round(gain * 10) / 10;
        drag.frequency = freq;
        drag.gain_db = gainRounded;

        // Throttled send. Mirrors AudioFader's 20 Hz outbound during
        // drag (~50 ms cadence) — fast enough that the audible/visible
        // change tracks the gesture, slow enough to keep the WS from
        // saturating with CFSP/CEBP echoes.
        const props = drag.usesGain
            ? { frequency: freq, gain_db: gainRounded }
            : { frequency: freq };
        this._eqBandDragSendThrottled(drag.bandIdx, props);
    },

    _eqBandDragSendThrottled(bandIdx, props) {
        const drag = this.eqBandDrag;
        if (!drag) return;
        const t = drag.throttle;
        const SEND_INTERVAL_MS = 50;
        const now = performance.now();
        const dt = now - t.lastSentAt;
        t.pendingProps = props;
        if (dt >= SEND_INTERVAL_MS) {
            t.lastSentAt = now;
            this.setBandProp(bandIdx, props);
            t.pendingProps = null;
            if (t.timer) {
                clearTimeout(t.timer);
                t.timer = null;
            }
        } else if (!t.timer) {
            // Trailing edge — schedule one send for the end of the
            // throttle window so a pause-mid-drag still delivers the
            // latest value.
            t.timer = setTimeout(() => {
                const cur = this.eqBandDrag;
                if (!cur) return;
                cur.throttle.timer = null;
                if (cur.throttle.pendingProps) {
                    this.setBandProp(bandIdx, cur.throttle.pendingProps);
                    cur.throttle.lastSentAt = performance.now();
                    cur.throttle.pendingProps = null;
                }
            }, SEND_INTERVAL_MS - dt);
        }
    },

    eqReleaseBandDrag(ev) {
        if (!this.eqBandDrag) return;
        const drag = this.eqBandDrag;
        const svgEl = ev.currentTarget;
        try { svgEl.releasePointerCapture(ev.pointerId); } catch (e) {}
        // Cancel any pending trailing-edge timer; we send the final
        // value here directly.
        if (drag.throttle.timer) {
            clearTimeout(drag.throttle.timer);
            drag.throttle.timer = null;
        }
        const finalProps = drag.usesGain
            ? { frequency: drag.frequency, gain_db: drag.gain_db }
            : { frequency: drag.frequency };
        this.setBandProp(drag.bandIdx, finalProps);
        this.eqBandDrag = null;
    },
    /* Grid + label + marker layers are emitted as raw SVG strings and
       injected via x-html. Reason: Alpine template-x-for nested inside
       an svg parses the template body in HTML namespace, so cloned
       line / text / g / circle nodes are invisible HTML elements
       rather than SVG ones. Setting innerHTML on a g that is already
       in SVG namespace parses the string as SVG. */
    eqSvgGrid() {
        // NOTE: every attribute uses single quotes. The whole x-data
        // block is itself a double-quoted attribute, so any literal
        // double-quote character in JS source — including inside a
        // comment! — closes the attribute and dumps the rest of the
        // body to the page as text.
        const v = this.EQ_VIZ;
        const out = [];
        for (const g of this.eqGridGains) {
            const y = this.eqGainToY(g);
            const isZero = g === 0;
            const stroke = isZero ? 'oklch(var(--bc) / 0.5)' : 'oklch(var(--bc) / 0.22)';
            const sw = isZero ? 1.2 : 0.8;
            const label = (g >= 0 ? '+' : '') + g;
            out.push(
                `<line x1='${v.PAD_L}' x2='${v.W - v.PAD_R}' y1='${y}' y2='${y}' `
                + `stroke='${stroke}' stroke-width='${sw}'/>`,
                `<text x='${v.PAD_L - 6}' y='${y + 4}' text-anchor='end' `
                + `fill='oklch(var(--bc) / 0.78)' font-size='12'>${label}</text>`,
                `<text x='${v.W - v.PAD_R + 6}' y='${y + 4}' text-anchor='start' `
                + `fill='oklch(var(--bc) / 0.78)' font-size='12'>${label}</text>`,
            );
        }
        for (const f of this.eqGridFreqs) {
            const x = this.eqFreqToX(f);
            out.push(
                `<line x1='${x}' x2='${x}' y1='${v.PAD_T}' y2='${v.H - v.PAD_B}' `
                + `stroke='oklch(var(--bc) / 0.18)' stroke-width='0.8'/>`,
                `<text x='${x}' y='${v.H - 8}' text-anchor='middle' `
                + `fill='oklch(var(--bc) / 0.78)' font-size='12'>${this.eqFreqLabel(f)}</text>`,
            );
        }
        return out.join('');
    },
    eqSvgMarkers(subject) {
        if (!subject || !subject.eq_bands) return '';
        const out = [];
        for (const band of subject.eq_bands) {
            const pos = this.eqMarkerPos(band);
            const opacity = band.enabled ? 1 : 0.4;
            out.push(
                `<g opacity='${opacity}'>`,
                `<circle cx='${pos.x}' cy='${pos.y}' r='13' `
                + `fill='oklch(var(--p) / 0.18)' stroke='oklch(var(--p))' stroke-width='1.6'/>`,
                `<text x='${pos.x}' y='${pos.y + 4.5}' text-anchor='middle' `
                + `fill='oklch(var(--p))' font-size='13' font-weight='700'>${band.index + 1}</text>`,
                `</g>`,
            );
        }
        return out.join('');
    },
    // --- Profile section-selection dialog state ---
    // mode: 'save' or 'load'. Open on click of the Save State / Load State
    // buttons; the same modal markup serves both with mode-specific differences.
    profileDialogOpen: false,
    profileDialogMode: 'save',
    profileDialogTitle: '',
    profileBusy: false,
    profileBusyMessage: '',
    profileDescriptor: null,
    profileSelections: {},
    // Which M/E tab's section panel is showing. Set to the first
    // available tab when a descriptor loads (AtemProfile.openSaveDialog
    // / openLoadDialog); clicking a greyed tab is a no-op.
    profileActiveMeTab: 0,
    profileApplyResult: null,
    profilePendingXml: null,         // for load: parsed-XML response stash
    profilePendingImages: null,      // for load: per-image FileList from picker
    // Pending video-mode change confirmation. Null = no dialog open.
    // Set by the Video Mode dropdown's @change handler with
    // {fromId, fromLabel, toId, toLabel}; the modal reads it to display
    // the from/to labels. Cleared by Cancel; cleared + command fires by Confirm.
    pendingVideoModeChange: null,
    // Note: capture session id, progress payload, and cancelling flag
    // live on Alpine.store('atem') (see atem_control.js) rather than
    // here, so the WebSocket message handler in atem_control.js — which
    // only has the store — can write to the same object the modal
    // template reads via ``$store.atem.profileCapture*``.
    deleteSlotIdx: null,
    replaceSlotIdx: null,
    pendingFile: null,
    dragOverSlotIdx: null,
    dragSlotIdx: null,
    dragOverPlayerIdx: null,
    _dragWatchdog: null,
    isFileDrag(event) {
        const types = event.dataTransfer && event.dataTransfer.types;
        if (!types) return false;
        for (let i = 0; i < types.length; i++) {
            if (types[i] === 'Files') return true;
        }
        return false;
    },
    // dragleave isn't reliable when the drag exits the browser window or
    // the OS cancels the drag — the slot's overlay can stick. Each
    // dragover arms a 300ms watchdog; if no dragover fires by then, clear
    // both drag-over indices. A live drag fires dragover at ~30 Hz so the
    // watchdog never trips during real interaction.
    _armDragOverWatchdog() {
        if (this._dragWatchdog) clearTimeout(this._dragWatchdog);
        this._dragWatchdog = setTimeout(() => {
            this.dragOverSlotIdx = null;
            this.dragOverPlayerIdx = null;
            this._dragWatchdog = null;
        }, 300);
    },
    onSlotDragStart(event, slot) {
        this.dragSlotIdx = slot.index;
        if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = 'link';
            event.dataTransfer.setData('application/x-atem-slot', String(slot.index));
            event.dataTransfer.setData('text/plain', 'atem-slot:' + slot.index);
        }
    },
    onSlotDragEnd() {
        this.dragSlotIdx = null;
        this.dragOverPlayerIdx = null;
    },
    onSlotDrop(event, slot) {
        this.dragOverSlotIdx = null;
        const files = event.dataTransfer && event.dataTransfer.files;
        if (!files || files.length === 0) return;
        const file = files[0];
        if (!file.type || !file.type.startsWith('image/')) {
            alert('Only image files can be uploaded to the media pool.');
            return;
        }
        if (this.$store.atem.isSlotUploading(slot.index)) return;
        if (slot.isUsed) {
            this.replaceSlotIdx = slot.index;
            this.pendingFile = file;
        } else {
            this.$store.atem.uploadSlot(slot.index, file);
        }
    },
    onPlayerDrop(event, player) {
        this.dragOverPlayerIdx = null;
        let slotIdx = null;
        const raw = event.dataTransfer && event.dataTransfer.getData('application/x-atem-slot');
        if (raw !== '' && raw !== null && raw !== undefined) {
            const parsed = parseInt(raw, 10);
            if (!isNaN(parsed)) slotIdx = parsed;
        }
        if (slotIdx === null && this.dragSlotIdx !== null) {
            slotIdx = this.dragSlotIdx;
        }
        this.dragSlotIdx = null;
        if (slotIdx === null) return;
        ATEMControl.cmd('set_media_player_still', { player: player.index, slot: slotIdx });
        this.$store.atem.optimisticSetPlayerStill(player.index, slotIdx);
    },
    confirmReplace() {
        if (this.replaceSlotIdx !== null && this.pendingFile) {
            this.$store.atem.uploadSlot(this.replaceSlotIdx, this.pendingFile);
        }
        this.replaceSlotIdx = null;
        this.pendingFile = null;
    },
    cancelReplace() {
        this.replaceSlotIdx = null;
        this.pendingFile = null;
    }
    }));
});
