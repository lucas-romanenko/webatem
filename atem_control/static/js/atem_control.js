/**
 * ATEM Control JavaScript - Complete Version with DVE and Individual Transition Rate Support
 * Updated with DVE Push/Squeeze functionality and individual rate countdown support for Mix, Dip, Wipe, DVE, and Stinger
 * FIXED: DVE system now uses ATEM state instead of local state, corrected Push styles to 24-31
 * FIXED: Added USK types array support
 */

// ---------------------------------------------------------------------------
// AudioMeters — direct DOM updater for the Fairlight meter elements.
// Bypasses Alpine reactivity entirely: meters arrive at ~25 Hz per strip
// and 25 Hz for the master, and routing them through Alpine would cause
// reactive thrash across the page. The audio panel template marks each
// meter cell with `data-strip-id` + `data-meter-channel` (l|r), and the
// inner `.audio-meter-fill` / `.audio-meter-peak` divs receive direct
// height/bottom updates here.
//
// dB range from the FMLv/FDLv decoders is roughly -60..0 dB. We map to
// 0..100% height with a slight log-style emphasis on the loud end so
// the bars track the way audio engineers expect (most of the visible
// range used by speech/music sitting around -20..-6 dB).
// ---------------------------------------------------------------------------
window.AudioMeters = {
    // Wire is 0.01 dB in -10000..0, decoded by pyatem to -100..0 dB.
    // The bar's visible range spans the full -100..0; the labelled
    // scale (in the audio modal) only goes 0..-50, which lands at
    // ~96 % of the bar — the tail below -50 is the "off-scale low"
    // territory that Software Control also leaves unlabelled.
    METER_FLOOR_DB: -100,
    METER_CEIL_DB: 0,

    // PPM-ish ballistics, modelled on IEC 60268-10 type IIa
    // (Software Control behaves the same way):
    //   - bar snaps up instantly on louder samples (no attack
    //     smoothing — a fast transient must be visible the frame
    //     it arrives).
    //   - bar decays at 13 dB/s when sample drops (≈20 dB drop in
    //     1.5 s).
    //   - peak indicator latches the highest recent peak, holds for
    //     PEAK_HOLD_MS, then falls at the same rate as the bar.
    // Without this, raw FMLv samples (~25 Hz) make the bar blip on/
    // off rather than glide.
    DECAY_DB_PER_S: 13,
    PEAK_HOLD_MS: 1500,
    PEAK_FALL_DB_PER_S: 13,

    // Set window.AudioMeters.debug = true in the console to log each
    // meter event + DOM lookup result. Useful for diagnosing
    // "FDLv works but FMLv doesn't" — the most likely cause is a
    // strip_id format mismatch between the python `:data-strip-id`
    // and the FMLv decoder's `strip_id`.
    debug: false,

    _stats: { strip: 0, master: 0, missCells: 0 },

    // Last-seen meter snapshot per strip_id (and '_master'). Use the
    // peek() / peekAll() helpers from the console to inspect live
    // values — useful for diagnosing "this meter shows signal but
    // ATEM Software Control says it's silent" without wading through
    // a debug-true console flood.
    _last: {},

    // Per-cell ballistics state. Key is `${stripId}|${channel}`.
    // Each entry: { sampleDb, currentDb, peakDb, peakHoldUntil }.
    _cellState: {},
    _rafId: null,
    _lastTickMs: 0,
    _tickBound: null,

    // Three-segment piecewise log-style mapping that matches Software
    // Control's bar (and label) positions:
    //   0 dB    → 100 % bar fill (full)
    //   -20 dB  → 50 %  (top half = the "loud" 0..-20 range, expanded)
    //   -50 dB  → 4 %   (bottom half = the -20..-50 range, compressed)
    //   -100 dB → 0 %   (tail below the labelled scale, fades to floor)
    // Returns the bar-fill percent for level meters (anchored at the
    // bottom). Label positions in the template are derived from the
    // inverse: pos-from-top = 100 - _dbToPct(label_db).
    _dbToPct(db) {
        if (typeof db !== 'number' || isNaN(db)) return 0;
        if (db >= 0) return 100;
        const absDb = Math.min(100, -db);
        let fromTop;
        if (absDb <= 20) {
            fromTop = (absDb / 20) * 50;
        } else if (absDb <= 50) {
            fromTop = 50 + ((absDb - 20) / 30) * 46;
        } else {
            fromTop = 96 + ((absDb - 50) / 50) * 4;
        }
        return Math.max(0, Math.min(100, 100 - fromTop));
    },

    _paintCellDom(stripId, channel, currentDb, peakDb) {
        const cell = document.querySelector(
            `.audio-meter-bg[data-strip-id="${stripId}"][data-meter-channel="${channel}"]`
        );
        if (!cell) {
            this._stats.missCells++;
            if (this.debug && this._stats.missCells < 8) {
                console.warn(`AudioMeters: no cell for strip_id=${stripId} ch=${channel}`);
            }
            return false;
        }
        const fill = cell.querySelector('.audio-meter-fill');
        const peakBar = cell.querySelector('.audio-meter-peak');
        if (fill) {
            fill.style.height = this._dbToPct(currentDb) + '%';
            // Tint: green ≤ -12, yellow -12..-6, red ≥ -6.
            fill.className = fill.className.replace(/\bis-(warn|clip)\b/g, '').trim();
            if (currentDb >= -6) fill.className += ' is-clip';
            else if (currentDb >= -12) fill.className += ' is-warn';
        }
        if (peakBar) peakBar.style.bottom = this._dbToPct(peakDb) + '%';
        return true;
    },

    _recordSample(stripId, channel, level, peak) {
        const key = stripId + '|' + channel;
        let state = this._cellState[key];
        if (!state) {
            state = {
                sampleDb: this.METER_FLOOR_DB,
                currentDb: this.METER_FLOOR_DB,
                peakDb: this.METER_FLOOR_DB,
                peakHoldUntil: 0,
            };
            this._cellState[key] = state;
        }
        state.sampleDb = (typeof level === 'number') ? level : this.METER_FLOOR_DB;

        // Instant attack: a louder sample wins immediately so transients
        // aren't smoothed away by the decay envelope.
        if (state.sampleDb > state.currentDb) {
            state.currentDb = state.sampleDb;
        }

        // Peak indicator latch — only re-arm if the new peak beats the
        // currently-held one. Otherwise it stays put until the hold
        // window expires.
        const peakSample = (typeof peak === 'number') ? peak : this.METER_FLOOR_DB;
        if (peakSample > state.peakDb) {
            state.peakDb = peakSample;
            state.peakHoldUntil = performance.now() + this.PEAK_HOLD_MS;
        }

        // Paint synchronously so the attack edge is visible the same
        // frame the FMLv arrived. Decay is handled by the rAF loop.
        this._paintCellDom(stripId, channel, state.currentDb, state.peakDb);

        // Make sure the decay loop is running.
        if (this._rafId === null) {
            if (!this._tickBound) this._tickBound = this._tick.bind(this);
            this._lastTickMs = performance.now();
            this._rafId = requestAnimationFrame(this._tickBound);
        }
    },

    _tick(now) {
        // dt is capped at 100 ms to keep a tab returning from
        // background-throttling from doing one massive jump.
        const dt = Math.min((now - this._lastTickMs) / 1000, 0.1);
        this._lastTickMs = now;

        let active = false;
        for (const key in this._cellState) {
            const state = this._cellState[key];
            let changed = false;

            // Bar release.
            if (state.currentDb > state.sampleDb) {
                state.currentDb = Math.max(
                    state.sampleDb,
                    state.currentDb - this.DECAY_DB_PER_S * dt
                );
                changed = true;
            }

            // Peak hold + fall.
            if (now > state.peakHoldUntil && state.peakDb > state.currentDb) {
                state.peakDb = Math.max(
                    state.currentDb,
                    state.peakDb - this.PEAK_FALL_DB_PER_S * dt
                );
                changed = true;
            }

            if (changed) {
                // Stage cells (modal Input/Output meters) carry their
                // identity as state._stripId / _stage / _channel so the
                // tick loop knows to paint into a different DOM target
                // than the strip-row meter for the same strip_id.
                if (state._stage) {
                    this._paintStageCellDom(
                        state._stripId, state._stage, state._channel,
                        state.currentDb, state.peakDb
                    );
                } else {
                    const sep = key.indexOf('|');
                    this._paintCellDom(
                        key.slice(0, sep),
                        key.slice(sep + 1),
                        state.currentDb,
                        state.peakDb
                    );
                }
                active = true;
            }
        }

        if (active) {
            this._rafId = requestAnimationFrame(this._tickBound);
        } else {
            this._rafId = null;
        }
    },

    // Drop every cell's sample floor (and release the peak holds) so the
    // decay loop runs the bars down to silence. Called when the meter
    // stream dies — WebSocket close or in-page ATEM switch.
    // Without this the bars freeze at the last received level and read
    // as a live signal.
    reset() {
        let any = false;
        for (const key in this._cellState) {
            const state = this._cellState[key];
            state.sampleDb = this.METER_FLOOR_DB;
            state.peakHoldUntil = 0;
            any = true;
        }
        if (any && this._rafId === null) {
            if (!this._tickBound) this._tickBound = this._tick.bind(this);
            this._lastTickMs = performance.now();
            this._rafId = requestAnimationFrame(this._tickBound);
        }
    },

    paintStrip(data) {
        if (!data || !data.input) return;
        this._stats.strip++;
        this._last[data.strip_id] = data;
        if (this.debug && this._stats.strip <= 5) {
            console.debug('AudioMeters.paintStrip', data.strip_id, data.level);
        }
        // FMLv carries three meter stages per strip:
        //   input  — pre-dynamics
        //   output — post-dynamics, PRE-fader
        //   level  — POST-fader (what Software Control shows on the
        //            strip meter; reflects fader position + dynamics)
        // We paint ``level`` so a strip with a low fader naturally
        // shows quiet, matching what an operator hears.
        const m = data.level || data.output || data.input;
        this._recordSample(data.strip_id, 'l', m.l, m.peak_l);
        this._recordSample(data.strip_id, 'r', m.r, m.peak_r);
        this._paintLevelText(data.strip_id, m);

        // Dynamics modal: paint pre-dynamics input meters, post-fader
        // output meters (separate elements from the strip-row meter so
        // both views can be live at once), and the three gain-reduction
        // meters (E/C/L). Selectors target elements with the dynamics-
        // modal data attributes, so this is a no-op when the modal
        // isn't open.
        if (data.input) {
            this._paintStageCells(data.strip_id, 'input', data.input);
        }
        if (data.level) {
            this._paintStageCells(data.strip_id, 'output', data.level);
        }
        this._paintGRCells(data.strip_id, data);
    },

    // Modal input/output meter painter. Same value pipeline as the
    // strip-row meter (decay envelope, peak hold) but keyed on a
    // separate ``stage`` axis so input and output bars don't clobber
    // each other or the strip-row level meter.
    _paintStageCells(stripId, stage, m) {
        if (!m) return;
        const lKey = stripId + '|' + stage + '|l';
        const rKey = stripId + '|' + stage + '|r';
        this._recordStageSample(stripId, stage, 'l', lKey, m.l, m.peak_l);
        this._recordStageSample(stripId, stage, 'r', rKey, m.r, m.peak_r);
    },

    _recordStageSample(stripId, stage, channel, key, level, peak) {
        let state = this._cellState[key];
        if (!state) {
            state = {
                sampleDb: this.METER_FLOOR_DB,
                currentDb: this.METER_FLOOR_DB,
                peakDb: this.METER_FLOOR_DB,
                peakHoldUntil: 0,
                _stage: stage,
                _stripId: stripId,
                _channel: channel,
            };
            this._cellState[key] = state;
        }
        state.sampleDb = (typeof level === 'number') ? level : this.METER_FLOOR_DB;
        if (state.sampleDb > state.currentDb) state.currentDb = state.sampleDb;
        const peakSample = (typeof peak === 'number') ? peak : this.METER_FLOOR_DB;
        if (peakSample > state.peakDb) {
            state.peakDb = peakSample;
            state.peakHoldUntil = performance.now() + this.PEAK_HOLD_MS;
        }
        this._paintStageCellDom(stripId, stage, channel,
                                 state.currentDb, state.peakDb);
        if (this._rafId === null) {
            if (!this._tickBound) this._tickBound = this._tick.bind(this);
            this._lastTickMs = performance.now();
            this._rafId = requestAnimationFrame(this._tickBound);
        }
    },

    _paintStageCellDom(stripId, stage, channel, currentDb, peakDb) {
        const cell = document.querySelector(
            `.audio-meter-bg[data-strip-id="${stripId}"]` +
            `[data-meter-stage="${stage}"][data-meter-channel="${channel}"]`
        );
        if (!cell) return false;
        const fill = cell.querySelector('.audio-meter-fill');
        const peakBar = cell.querySelector('.audio-meter-peak');
        if (fill) {
            fill.style.height = this._dbToPct(currentDb) + '%';
            fill.className = fill.className.replace(/\bis-(warn|clip)\b/g, '').trim();
            if (currentDb >= -6) fill.className += ' is-clip';
            else if (currentDb >= -12) fill.className += ' is-warn';
        }
        if (peakBar) peakBar.style.bottom = this._dbToPct(peakDb) + '%';
        return true;
    },

    // Gain-reduction meters (E/C/L). Each is a single bar — no L/R, no
    // peak hold (GR by definition is always ≤ 0 dB so there's no
    // "transient" to latch). Wire values are already in dB
    // (FMLv/FDLv parsers divide the i16 wire field by 100 — see
    // FairlightMeterLevelsField._level). Bars grow DOWNWARDS from the
    // top of the cell since GR is cut from the signal.
    //
    // Uses the same -60..0 dB linear mapping as the level meters
    // (_dbToPct), so the dB labels rendered alongside Input / Output /
    // GR all line up at the same positions. Software Control has a tiny
    // tail below -50 dB before the bar bottoms out, which falls out of
    // this naturally: -50 dB lands at 83.3% of the bar height.
    _paintGRCells(stripId, data) {
        const blocks = [
            ['expander', 'expander_gr'],
            ['compressor', 'compressor_gr'],
            ['limiter', 'limiter_gr'],
        ];
        for (const [block, fieldName] of blocks) {
            const grDb = data[fieldName];
            if (grDb === undefined || grDb === null) continue;
            const cell = document.querySelector(
                `.dyn-gr-bg[data-strip-id="${stripId}"][data-gr-block="${block}"]`
            );
            if (!cell) continue;
            const fill = cell.querySelector('.dyn-gr-fill');
            if (!fill) continue;
            // ATEM reports GR as a positive dB amount (e.g. 6.49 = 6.49 dB
            // of reduction). Same piecewise mapping the level meters use,
            // applied to the magnitude:
            //   0..20 dB reduction → 0..50 % bar fill
            //   20..50 dB reduction → 50..100 % bar fill
            const reduceDb = Math.abs(grDb);
            let pct;
            if (reduceDb <= 20) {
                pct = (reduceDb / 20) * 50;
            } else {
                pct = 50 + ((reduceDb - 20) / 30) * 50;
            }
            fill.style.height = Math.max(0, Math.min(100, pct)) + '%';
        }
    },

    paintMaster(data) {
        if (!data) return;
        this._stats.master++;
        this._last['_master'] = data;
        // Master FDLv has the same three stages; ``level`` is post-fader.
        const m = data.level || data.output || data.input;
        if (!m) return;
        this._recordSample('_master', 'l', m.l, m.peak_l);
        this._recordSample('_master', 'r', m.r, m.peak_r);
        this._paintLevelText('_master', m);
    },

    // Top "dB" cell — live meter peak readout. Mirrors ATEM Software
    // Control: shows the louder of L/R peak when there's audible
    // signal, empty when below the meter floor. Hidden by direct DOM
    // mutation rather than Alpine binding so it tracks the ~25 Hz
    // meter rate without reactive churn.
    _paintLevelText(stripId, m) {
        const node = document.querySelector(
            `[data-strip-id-meter-db="${stripId}"]`
        );
        if (!node) return;
        const peak = Math.max(m.peak_l ?? -100, m.peak_r ?? -100);
        if (peak <= this.METER_FLOOR_DB) {
            node.textContent = '';
            node.classList.remove('text-error', 'text-warning', 'text-success');
            return;
        }
        node.textContent = peak.toFixed(2);
        node.classList.remove('text-error', 'text-warning', 'text-success');
        if (peak >= -6)        node.classList.add('text-error');
        else if (peak >= -12)  node.classList.add('text-warning');
        else                   node.classList.add('text-success');
    },

    // Inspector: call from console to see what's being received.
    stats() { return { ...this._stats }; },

    // Peek the last-seen meter snapshot for a strip. Pass strip_id
    // (e.g. '1301.0' for Mic) or '_master'. Pass nothing to dump all.
    peek(stripId) { return this._last[stripId] ?? null; },
    peekAll() {
        const out = {};
        for (const [k, v] of Object.entries(this._last)) {
            out[k] = { input: v.input, output: v.output, level: v.level };
        }
        return out;
    },
};

// ---------------------------------------------------------------------------
// AudioFader — custom vertical fader matching the T-Bar idiom on this page.
//
// Why custom: native <input type="range"> with -webkit-slider-vertical /
// writing-mode is unreliable across browsers (jumpy thumb sizing in WebKit,
// different geometry in Firefox), and Alpine's `:value="strip.volume_db"`
// binding fights the user's drag every time the polling state push echoes
// back a stale volume mid-motion. The result felt jittery and laggy.
//
// Design (mirrors window.ATEMControl.setupTBarInteraction):
//   - Pointer events with setPointerCapture so the drag survives leaving
//     the element. Works for mouse, touch, and pen with one code path.
//   - DOM-direct paint of `handle.style.top` during drag — no Alpine
//     reactivity in the hot path.
//   - _draggingStrip flag — `update(value)` is a no-op for the strip
//     currently being dragged so server echoes can't yank the handle.
//   - Throttled outbound sends (~33 Hz) plus a final send on pointerup.
//   - dblclick → reset to 0 dB; Shift held → ¼ sensitivity (fine adjust);
//     wheel → ±1 dB / ±0.25 dB with Shift.
//
// Public API (called from the audio-panel template via x-init / x-effect):
//   AudioFader.attach(trackEl, opts) -> ctrl    ← set up
//   AudioFader.update(stripId, value)            ← server-driven repaint
//
// opts: {
//   stripId,                       // 'source.subchannel' or '_master'
//   min, max,                      // dB range, e.g. -100 / +10
//   getValue: () => number|null,   // current value source-of-truth
//   send:    (value: number) => void,  // dispatch a WS command
//   onPreview?: (value: number) => void,  // optional — see below
// }
//
// Two send modes:
//   - **realtime (default)**: every drag tick fires opts.send(value) at
//     ~20 Hz so the model tracks the gesture live (channel volume, mute
//     transitions). Final value sent again on pointerup.
//   - **commit-on-release** (when opts.onPreview is provided): drag ticks
//     call opts.onPreview(value) for cheap local feedback (e.g. update a
//     local visualizer subject), and opts.send(value) fires *only once*
//     on pointerup. Use this when the model is expensive to update on
//     each tick — e.g. EQ gain, where every CFSP triggers a 200-point
//     SVG curve recompute via the FASP echo storm.
// ---------------------------------------------------------------------------
window.AudioFader = {
    SEND_INTERVAL_MS: 50,            // 20 Hz outbound during drag
    POST_RELEASE_MUTE_MS: 30000,     // safety net only — primary unmute is
                                     // wait-for-echo (clears in <1s once
                                     // the matching echo arrives). The
                                     // long fallback covers worst-case
                                     // queue drain when the audio panel
                                     // is open and ~350 msg/s of meter
                                     // traffic is pushing FASP echoes
                                     // back 10+s. Reduce to ~3s once
                                     // meter frames are bundled.
    ECHO_TOLERANCE_DB: 0.1,          // accept echo if within this many dB of
                                     // the released value — clears the mute
                                     // early so the slider tracks the server
                                     // again as soon as the queue's drained,
                                     // rather than always waiting the full
                                     // safety timeout.
    _controllers: {},                // stripId → ctrl
    _draggingStrip: null,
    _muteUntil: {},                  // stripId → performance.now() expiry
    _expectedValue: {},              // stripId → released value, matched
                                     // against echoes to clear the mute.

    isDragging(stripId) { return this._draggingStrip === stripId; },

    update(stripId, value) {
        // Server-driven repaint. Two reasons to refuse:
        //  1. The user is dragging this strip right now.
        //  2. They JUST released — there's a queue of in-flight FASP
        //     echoes that, if we honoured them, would repaint the
        //     handle through the historical drag values (the "replay"
        //     symptom). Stay muted until the echo value matches what
        //     the operator released at — that signals the queue has
        //     drained and it's safe to track the server again.
        if (this._draggingStrip === stripId) return;
        const muteEnd = this._muteUntil[stripId];
        if (muteEnd && performance.now() < muteEnd) {
            const expected = this._expectedValue[stripId];
            if (expected !== undefined &&
                Math.abs(value - expected) <= this.ECHO_TOLERANCE_DB) {
                // Echo matched — drag's done draining, unmute and apply.
                delete this._muteUntil[stripId];
                delete this._expectedValue[stripId];
            } else {
                return;
            }
        } else if (muteEnd) {
            // Safety timeout fired without echo match. Accept what arrives.
            delete this._muteUntil[stripId];
            delete this._expectedValue[stripId];
        }
        const ctrl = this._controllers[stripId];
        if (ctrl) ctrl.update(value);
    },

    attach(trackEl, opts) {
        const handleEl = trackEl.querySelector('.audio-fader-handle');
        if (!handleEl) {
            console.warn('AudioFader: no .audio-fader-handle inside', trackEl);
            return null;
        }
        const fader = this;
        const stripId = opts.stripId;
        // Lazy lookup with cache. Per-strip rows use a reactive Alpine
        // binding (`:data-strip-id-fader-value="strip.strip_id"`) which
        // may not have been processed by Alpine when this attach() runs
        // — descendants inside an x-for can finish init AFTER the
        // ancestor's x-init. A captured-at-attach reference is null in
        // that case and paint silently no-ops the textContent set,
        // leaving the dB readout stuck on whatever Alpine eventually
        // paints once. Re-query each paint and cache the first hit.
        // Mirrors AudioMeters._paintLevelText.
        let cachedLabelEl = null;
        const getLabelEl = () => {
            if (cachedLabelEl && document.contains(cachedLabelEl)) {
                return cachedLabelEl;
            }
            cachedLabelEl = document.querySelector(
                `[data-strip-id-fader-value="${stripId}"]`
            );
            return cachedLabelEl;
        };
        // taper > 1 expands the top of the range (around 0 dB) and
        // compresses the bottom (-100..-30 dB) — matches a real audio
        // fader law and aligns the handle position with what ATEM
        // Software Control shows. Default 1 = linear (no behavior
        // change for callers that don't pass it).
        const taper = (typeof opts.taper === 'number' && opts.taper > 0)
                        ? opts.taper : 1;

        let startY = 0;
        let startValue = 0;
        let lastSentAt = 0;
        let pendingValue = null;
        let trailingTimer = null;

        const formatLabel = (v) => {
            if (v === null || v === undefined || isNaN(v) || v <= opts.min) return '-inf';
            return (v >= 0 ? '+' : '') + v.toFixed(2);
        };

        const valueToVisualPct = (v) => {
            const clamped = Math.max(opts.min, Math.min(opts.max, v));
            const linearPct = (clamped - opts.min) / (opts.max - opts.min);
            return Math.pow(linearPct, taper);
        };
        const visualPctToValue = (pct) => {
            const clampedPct = Math.max(0, Math.min(1, pct));
            const linearPct = Math.pow(clampedPct, 1 / taper);
            return opts.min + linearPct * (opts.max - opts.min);
        };

        const paint = (value) => {
            const v = (value === null || value === undefined || isNaN(value))
                        ? opts.min : value;
            // Top of track = max value, so flip the visual pct.
            const pct = 1 - valueToVisualPct(v);
            handleEl.style.top = (pct * 100) + '%';
            const labelEl = getLabelEl();
            if (labelEl) labelEl.textContent = formatLabel(value);
        };

        const sendThrottled = (value) => {
            pendingValue = value;
            const now = performance.now();
            const dt = now - lastSentAt;
            if (dt >= fader.SEND_INTERVAL_MS) {
                lastSentAt = now;
                opts.send(value);
                pendingValue = null;
                if (trailingTimer) {
                    clearTimeout(trailingTimer);
                    trailingTimer = null;
                }
            } else if (!trailingTimer) {
                // schedule the trailing edge so a pause-mid-drag still
                // delivers the latest value
                trailingTimer = setTimeout(() => {
                    trailingTimer = null;
                    if (pendingValue !== null) {
                        lastSentAt = performance.now();
                        opts.send(pendingValue);
                        pendingValue = null;
                    }
                }, fader.SEND_INTERVAL_MS - dt);
            }
        };

        const computeValueFromDelta = (clientY, shift) => {
            const trackH = trackEl.clientHeight;
            if (trackH <= 0) return startValue;
            // Drag is linear in VISUAL space — pixel motion translates
            // directly to handle position; the taper inversion turns
            // that into a value. This keeps the drag feel uniform along
            // the track instead of getting sluggish near the bottom or
            // jumpy near the top, even with an aggressive taper.
            const startPct = valueToVisualPct(startValue);
            const deltaPx = clientY - startY;
            const sens = shift ? 0.25 : 1.0;
            const dPct = -(deltaPx / trackH) * sens;  // up-drag = louder
            const newPct = Math.max(0, Math.min(1, startPct + dPct));
            return Math.max(opts.min, Math.min(opts.max, visualPctToValue(newPct)));
        };

        const onPointerDown = (ev) => {
            // Skip secondary buttons (right-click, middle-click)
            if (ev.button !== undefined && ev.button !== 0) return;
            ev.preventDefault();
            try { trackEl.setPointerCapture(ev.pointerId); } catch (e) {}
            trackEl.classList.add('is-dragging');
            fader._draggingStrip = stripId;
            // New drag — abandon any post-release mute window from the
            // previous one so this drag starts from current state.
            delete fader._muteUntil[stripId];
            delete fader._expectedValue[stripId];
            startY = ev.clientY;
            startValue = opts.getValue() ?? opts.min;
        };

        const onPointerMove = (ev) => {
            if (fader._draggingStrip !== stripId) return;
            const value = computeValueFromDelta(ev.clientY, ev.shiftKey);
            paint(value);
            if (opts.onPreview) {
                // Commit-on-release mode: don't dispatch to server during
                // drag; the caller updates a local model so the rest of
                // the UI (e.g. EQ visualizer) tracks the gesture without
                // round-tripping through ATEM.
                opts.onPreview(value);
            } else {
                sendThrottled(value);
            }
        };

        const onPointerUp = (ev) => {
            if (fader._draggingStrip !== stripId) return;
            try { trackEl.releasePointerCapture(ev.pointerId); } catch (e) {}
            trackEl.classList.remove('is-dragging');
            fader._draggingStrip = null;
            // Cancel the trailing timer; we send a final value here.
            if (trailingTimer) {
                clearTimeout(trailingTimer);
                trailingTimer = null;
            }
            const value = computeValueFromDelta(ev.clientY, ev.shiftKey);
            paint(value);
            opts.send(value);
            pendingValue = null;
            lastSentAt = performance.now();
            // Open the post-release mute window. update() will accept the
            // first echo whose value matches `value` (within
            // ECHO_TOLERANCE_DB) and clear the mute early; the safety
            // timeout is the fallback.
            fader._expectedValue[stripId] = value;
            fader._muteUntil[stripId] =
                performance.now() + fader.POST_RELEASE_MUTE_MS;
        };

        const onDoubleClick = (ev) => {
            ev.preventDefault();
            paint(0);
            opts.send(0);
            fader._expectedValue[stripId] = 0;
            fader._muteUntil[stripId] =
                performance.now() + fader.POST_RELEASE_MUTE_MS;
        };

        const onWheel = (ev) => {
            ev.preventDefault();
            const cur = opts.getValue() ?? opts.min;
            const step = ev.shiftKey ? 0.25 : 1;
            const dir = ev.deltaY < 0 ? +1 : -1;
            const next = Math.max(opts.min, Math.min(opts.max, cur + dir * step));
            paint(next);
            opts.send(next);
            fader._expectedValue[stripId] = next;
            fader._muteUntil[stripId] =
                performance.now() + fader.POST_RELEASE_MUTE_MS;
        };

        trackEl.addEventListener('pointerdown', onPointerDown);
        trackEl.addEventListener('pointermove', onPointerMove);
        trackEl.addEventListener('pointerup', onPointerUp);
        trackEl.addEventListener('pointercancel', onPointerUp);
        trackEl.addEventListener('dblclick', onDoubleClick);
        trackEl.addEventListener('wheel', onWheel, { passive: false });

        // Initial paint reflects current value
        paint(opts.getValue());

        const ctrl = {
            update(value) {
                if (fader._draggingStrip === stripId) return;
                paint(value);
            },
            destroy() {
                trackEl.removeEventListener('pointerdown', onPointerDown);
                trackEl.removeEventListener('pointermove', onPointerMove);
                trackEl.removeEventListener('pointerup', onPointerUp);
                trackEl.removeEventListener('pointercancel', onPointerUp);
                trackEl.removeEventListener('dblclick', onDoubleClick);
                trackEl.removeEventListener('wheel', onWheel);
                if (trailingTimer) clearTimeout(trailingTimer);
                delete fader._controllers[stripId];
            },
        };
        // Tear down a previous attach for this strip if Alpine re-renders
        // (template x-for can rebuild DOM nodes).
        if (this._controllers[stripId]) this._controllers[stripId].destroy();
        this._controllers[stripId] = ctrl;
        return ctrl;
    },
};

// ---------------------------------------------------------------------------
// AudioKnob — drag-able knob + numeric input pair, sharing one value.
//
// Same shape as AudioFader (drag, throttled send, post-release mute, dblclick
// reset, wheel) plus a focus-aware numeric input. Two server-push problems
// the prior knob+input pair had:
//
//   1. The knob was wheel-only — drag did nothing — so adjusting input gain
//      or pan meant scrolling, which is awkward on a trackpad.
//   2. The <input> had `:value="strip.input_gain_db.toFixed(2)"`, which
//      overwrote the user's typing on every state push (200-400 ms during
//      idle, faster when other state churned). You could type "5" and have
//      it reverted before pressing Enter.
//
// API:
//   AudioKnob.attach(wrapperEl, opts) — wrapperEl must contain
//       `.audio-knob` and `.audio-numinput` children.
//   AudioKnob.update(key, value) — server-driven repaint.
//
// opts: {
//   key,                          // unique id, e.g. '1.0|input_gain'
//   min, max, step,               // value range (step rounds output)
//   defaultValue,                 // dblclick resets to this
//   getValue: () => number|null,  // current source-of-truth
//   send: (value) => void,        // dispatch the WS command
//   format?: (value) => string,   // numeric input display (default v.toFixed(2)).
//                                 // For non-numeric formats like 'X:1' the caller
//                                 // must also set the <input> to type="text".
//   parse?:  (raw) => number,     // inverse of format (default parseFloat)
// }
// ---------------------------------------------------------------------------
window.AudioKnob = {
    DRAG_PX_FULL_RANGE: 200,         // 200 px of vertical drag = full range
    SEND_INTERVAL_MS: 50,            // 20 Hz outbound during drag
    POST_RELEASE_MUTE_MS: 500,       // ignore echoes briefly after release
    _controllers: {},
    _draggingKey: null,
    _muteUntil: {},
    _editing: {},                    // key → true while numeric input has focus

    update(key, value) {
        const ctrl = this._controllers[key];
        if (!ctrl) return;
        if (this._draggingKey === key) return;
        // While the numeric input has focus, repaint the knob angle so the
        // operator sees the live value, but don't touch the input — that
        // would clobber what they're typing.
        if (this._editing[key]) {
            ctrl.updateAngleOnly(value);
            return;
        }
        const muteEnd = this._muteUntil[key];
        if (muteEnd && performance.now() < muteEnd) return;
        if (muteEnd) delete this._muteUntil[key];
        ctrl.update(value);
    },

    attach(wrapperEl, opts) {
        const knobEl = wrapperEl.querySelector('.audio-knob');
        const inputEl = wrapperEl.querySelector('.audio-numinput');
        if (!knobEl) {
            console.warn('AudioKnob: no .audio-knob inside', wrapperEl);
            return null;
        }
        const knob = this;
        const key = opts.key;
        // Two ways to specify the value <-> visual mapping:
        //   - opts.curve  : piecewise [[pct, value], ...] (exact)
        //   - opts.taper  : single power exponent (approximate)
        // Default 1 = linear.
        const taper = (typeof opts.taper === 'number' && opts.taper > 0)
                        ? opts.taper : 1;
        // Sort and stash the curve pre-computed in both directions so each
        // sample/drag tick is a constant-time slot lookup.
        const curveByPct = opts.curve
            ? [...opts.curve].sort((a, b) => a[0] - b[0])
            : null;
        const curveByValue = opts.curve
            ? [...opts.curve].sort((a, b) => a[1] - b[1])
            : null;

        let startY = 0;
        let startValue = 0;
        let lastSentAt = 0;

        const round = (v) => {
            if (!opts.step) return v;
            return Math.round(v / opts.step) * opts.step;
        };

        const valueToVisualPct = (v) => {
            const clamped = Math.max(opts.min, Math.min(opts.max, v));
            if (curveByValue) {
                // piecewise-linear interpolation between adjacent (pct, value)
                // points. Hits each breakpoint exactly so a calibrated curve
                // matches Software Control's indicator at every reference dB.
                for (let i = 1; i < curveByValue.length; i++) {
                    const [p2, v2] = curveByValue[i];
                    if (clamped <= v2) {
                        const [p1, v1] = curveByValue[i - 1];
                        if (v1 === v2) return p1;
                        const t = (clamped - v1) / (v2 - v1);
                        return p1 + t * (p2 - p1);
                    }
                }
                return curveByValue[curveByValue.length - 1][0];
            }
            const linearPct = (clamped - opts.min) / (opts.max - opts.min);
            return Math.pow(linearPct, taper);
        };
        const visualPctToValue = (pct) => {
            const clampedPct = Math.max(0, Math.min(1, pct));
            if (curveByPct) {
                for (let i = 1; i < curveByPct.length; i++) {
                    const [p2, v2] = curveByPct[i];
                    if (clampedPct <= p2) {
                        const [p1, v1] = curveByPct[i - 1];
                        if (p1 === p2) return v1;
                        const t = (clampedPct - p1) / (p2 - p1);
                        return v1 + t * (v2 - v1);
                    }
                }
                return curveByPct[curveByPct.length - 1][1];
            }
            const linearPct = Math.pow(clampedPct, 1 / taper);
            return opts.min + linearPct * (opts.max - opts.min);
        };

        const angleFor = (value) => {
            const v = (value === null || value === undefined || isNaN(value))
                        ? opts.min : value;
            const pct = valueToVisualPct(v);
            return -135 + pct * 270;
        };

        const paintAngle = (value) => {
            knobEl.style.setProperty('--angle', angleFor(value) + 'deg');
        };

        const paintInput = (value) => {
            if (!inputEl) return;
            // Don't trample what the user is typing.
            if (document.activeElement === inputEl) return;
            const v = (value === null || value === undefined || isNaN(value))
                        ? opts.min : value;
            inputEl.value = opts.format ? opts.format(v) : v.toFixed(2);
        };

        const paint = (value) => {
            paintAngle(value);
            paintInput(value);
        };

        const sendThrottled = (value) => {
            const now = performance.now();
            if (now - lastSentAt >= knob.SEND_INTERVAL_MS) {
                lastSentAt = now;
                opts.send(round(value));
            }
        };

        const valueFromDrag = (clientY, shift) => {
            // Drag is linear in VISUAL space (constant feel per pixel
            // regardless of where on the knob you are), then unmap
            // through the taper to a value. Otherwise dragging near
            // -100 dB would feel sluggish and dragging near +6 would
            // feel jumpy.
            const startPct = valueToVisualPct(startValue);
            const dy = startY - clientY;  // up = positive (louder/right)
            const sens = shift ? 0.25 : 1.0;
            const dPct = (dy / knob.DRAG_PX_FULL_RANGE) * sens;
            const newPct = Math.max(0, Math.min(1, startPct + dPct));
            return Math.max(opts.min, Math.min(opts.max, visualPctToValue(newPct)));
        };

        const onPointerDown = (ev) => {
            if (ev.button !== undefined && ev.button !== 0) return;
            ev.preventDefault();
            try { knobEl.setPointerCapture(ev.pointerId); } catch (e) {}
            knobEl.classList.add('is-dragging');
            knob._draggingKey = key;
            delete knob._muteUntil[key];
            startY = ev.clientY;
            startValue = opts.getValue() ?? opts.min;
        };

        const onPointerMove = (ev) => {
            if (knob._draggingKey !== key) return;
            const value = valueFromDrag(ev.clientY, ev.shiftKey);
            paint(value);
            sendThrottled(value);
        };

        const onPointerUp = (ev) => {
            if (knob._draggingKey !== key) return;
            try { knobEl.releasePointerCapture(ev.pointerId); } catch (e) {}
            knobEl.classList.remove('is-dragging');
            knob._draggingKey = null;
            const value = round(valueFromDrag(ev.clientY, ev.shiftKey));
            paint(value);
            opts.send(value);
            lastSentAt = performance.now();
            knob._muteUntil[key] = performance.now() + knob.POST_RELEASE_MUTE_MS;
        };

        const onDoubleClick = (ev) => {
            ev.preventDefault();
            const v = opts.defaultValue ?? 0;
            paint(v);
            opts.send(v);
            knob._muteUntil[key] = performance.now() + knob.POST_RELEASE_MUTE_MS;
        };

        const onWheel = (ev) => {
            ev.preventDefault();
            const cur = opts.getValue() ?? opts.min;
            const baseStep = opts.step ?? 1;
            const step = ev.shiftKey ? baseStep : baseStep * 4;
            const dir = ev.deltaY < 0 ? +1 : -1;
            const next = round(Math.max(opts.min, Math.min(opts.max,
                                                            cur + dir * step)));
            paint(next);
            opts.send(next);
            knob._muteUntil[key] = performance.now() + knob.POST_RELEASE_MUTE_MS;
        };

        knobEl.addEventListener('pointerdown', onPointerDown);
        knobEl.addEventListener('pointermove', onPointerMove);
        knobEl.addEventListener('pointerup', onPointerUp);
        knobEl.addEventListener('pointercancel', onPointerUp);
        knobEl.addEventListener('dblclick', onDoubleClick);
        knobEl.addEventListener('wheel', onWheel, { passive: false });

        // Numeric input — focus marks it as "editing"; Enter or blur
        // commits; Escape reverts.
        const onInputFocus = () => { knob._editing[key] = true; };
        const onInputBlur = () => {
            delete knob._editing[key];
            commitInput();
        };
        const onInputKeyDown = (ev) => {
            if (ev.key === 'Enter') {
                ev.preventDefault();
                inputEl.blur();          // triggers commit via onInputBlur
            } else if (ev.key === 'Escape') {
                ev.preventDefault();
                paintInput(opts.getValue());
                inputEl.blur();
            }
        };
        const commitInput = () => {
            if (!inputEl) return;
            const raw = inputEl.value;
            const v = opts.parse ? opts.parse(raw) : parseFloat(raw);
            if (isNaN(v)) {
                paintInput(opts.getValue());
                return;
            }
            const rounded = round(Math.max(opts.min, Math.min(opts.max, v)));
            const cur = opts.getValue();
            // Skip if no change, otherwise resync display.
            if (cur !== null && cur !== undefined && Math.abs(rounded - cur) < 1e-6) {
                paintInput(cur);
                return;
            }
            paint(rounded);
            opts.send(rounded);
            knob._muteUntil[key] = performance.now() + knob.POST_RELEASE_MUTE_MS;
        };

        if (inputEl) {
            inputEl.addEventListener('focus', onInputFocus);
            inputEl.addEventListener('blur', onInputBlur);
            inputEl.addEventListener('keydown', onInputKeyDown);
        }

        // Initial paint
        paint(opts.getValue());

        const ctrl = {
            update(value) { paint(value); },
            updateAngleOnly(value) { paintAngle(value); },
            destroy() {
                knobEl.removeEventListener('pointerdown', onPointerDown);
                knobEl.removeEventListener('pointermove', onPointerMove);
                knobEl.removeEventListener('pointerup', onPointerUp);
                knobEl.removeEventListener('pointercancel', onPointerUp);
                knobEl.removeEventListener('dblclick', onDoubleClick);
                knobEl.removeEventListener('wheel', onWheel);
                if (inputEl) {
                    inputEl.removeEventListener('focus', onInputFocus);
                    inputEl.removeEventListener('blur', onInputBlur);
                    inputEl.removeEventListener('keydown', onInputKeyDown);
                }
                delete knob._controllers[key];
                delete knob._editing[key];
            },
        };
        if (this._controllers[key]) this._controllers[key].destroy();
        this._controllers[key] = ctrl;
        return ctrl;
    },
};

// ---------------------------------------------------------------------------
// EQ_FILTER_ICONS — iconographic SVG previews for the six Fairlight EQ
// filter shapes, used by the band-panel dropdown in the EQ modal.
//
// Defined as raw strings here (outside the template / x-data) so they can
// have whatever stroke widths and curve geometry we want without risking a
// double-quote/<>/& leak into the x-data="..." attribute, which has bitten
// us three times now.
//
// The SVGs use stroke="currentColor" so the parent's `text-primary` /
// `text-base-content/70` Tailwind classes drive the icon colour. ViewBox
// is 60×20 (3:1) to match a small inline preview slot.
// ---------------------------------------------------------------------------
window.EQ_FILTER_ICONS = (function () {
    const wrap = (path) =>
        '<svg viewBox="0 0 60 20" fill="none" stroke="currentColor"'
        + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
        + ' style="width:100%;height:100%;display:block">'
        + '<path d="' + path + '"/></svg>';
    return {
        // High-flat then a smooth descent to a low-flat tail (boost-low → flat-high)
        LowShelf:  wrap('M 4 5 L 20 5 C 28 5 30 15 38 15 L 56 15'),
        // Mirror of LowShelf — low-flat then smooth rise to high-flat tail
        HighShelf: wrap('M 4 15 L 20 15 C 28 15 30 5 38 5 L 56 5'),
        // High-flat then sharp drop to floor — passes lows, blocks highs
        LowPass:   wrap('M 4 5 L 32 5 L 44 16 L 56 16'),
        // Floor on the left, sharp rise to high-flat — passes highs, blocks lows
        HighPass:  wrap('M 4 16 L 16 16 L 28 5 L 56 5'),
        // Bell — symmetric peak in the middle
        BandPass:  wrap('M 4 14 L 16 14 C 22 14 24 5 30 5 C 36 5 38 14 44 14 L 56 14'),
        // Notch — symmetric dip in the middle
        Notch:     wrap('M 4 5 L 22 5 C 26 5 27 16 30 16 C 33 16 34 5 38 5 L 56 5'),
    };
})();

// Global ATEM control with all original functionality + individual transition rates + DVE support
window.ATEMControl = {
    ws: null,
    isReconnecting: false, // Flag to track intentional reconnections
    connectingIP: null, // Track IP we're connecting to during reconnect
    isUserInitiatedConnection: false, // Flag to track user-initiated connections (like quickConnect)
    connectFriendlyErrors: false, // Quick-connect path: map raw failure messages to operator hints, never redirect
    
    // The Program/Preview bus rows render from the switcher's real
    // source list via $store.atem.busGeom() (state.sources, from
    // InPr) — there is no hardcoded source table anymore.

    // UPDATED: Extended rate countdown system with individual transition rates
    rateCountdowns: {
        me: { active: false, originalValue: null, inputId: 'transitionRate' },
        // Quick-panel per-DSK rates (dsk0/dsk1 -> DSK 1/2)
        dsk0: { active: false, originalValue: null, inputId: 'dskRate0' },
        dsk1: { active: false, originalValue: null, inputId: 'dskRate1' },
        ftb: { active: false, originalValue: null, inputId: 'ftbRate' },
        // Individual transition rate countdowns
        mix: { active: false, originalValue: null, inputId: 'mixRate' },
        dip: { active: false, originalValue: null, inputId: 'dipRate' },
        wipe: { active: false, originalValue: null, inputId: 'wipeRate' },
        dve: { active: false, originalValue: null, inputId: 'dveRate' },
        stinger: { active: false, originalValue: null, inputId: 'stingerRate' }
    },

    // RESTORED: T-Bar system
    tbarState: {
        position: 0,
        targetPosition: 0,
        isAnimating: false,
        isDragging: false,
        lastProgramSource: null,
        lastPreviewSource: null
    },
    tbarElements: {},
    
    // Transition styles
    transitionStyles: [
        { value: 0, label: 'MIX', color: 'green' },
        { value: 1, label: 'DIP', color: 'yellow' },
        { value: 2, label: 'WIPE', color: 'blue' },
        { value: 3, label: 'DVE', color: 'gray' },
        { value: 4, label: 'STING', color: 'red' }
    ],

    // CORRECTED: DVE Style Categories - Push styles corrected to 24-31
    dveStyleCategories: [
        { name: 'Push', id: 'push', styles: [24, 25, 26, 27, 28, 29, 30, 31] },
        { name: 'Squeeze', id: 'squeeze', styles: [16, 17, 18, 19, 20, 21, 22, 23] }
    ],

    // CORRECTED: DVE Style Names Map - Push styles corrected to 24-31
    dveStyleNames: {
        // Push styles (24-31)
        24: 'Push Top Left',
        25: 'Push Top',
        26: 'Push Top Right', 
        27: 'Push Left',
        28: 'Push Right',
        29: 'Push Bottom Left',
        30: 'Push Bottom',
        31: 'Push Bottom Right',
        
        // Squeeze styles (16-23)
        16: 'Squeeze Top Left',
        17: 'Squeeze Top',
        18: 'Squeeze Top Right',
        19: 'Squeeze Left', 
        20: 'Squeeze Right',
        21: 'Squeeze Bottom Left',
        22: 'Squeeze Bottom',
        23: 'Squeeze Bottom Right',

        34: 'Graphic Logo Wipe'
    },

    // NEW: Grid position mapping for 8-direction styles (excluding center)
    gridPositions: {
        // 3x3 grid positions (0-based indexing, excluding center position 4)
        0: 'topLeft',     // Top Left
        1: 'top',         // Top Center  
        2: 'topRight',    // Top Right
        3: 'left',        // Middle Left
        // 4: center (not used for directional styles)
        5: 'right',       // Middle Right
        6: 'bottomLeft',  // Bottom Left
        7: 'bottom',      // Bottom Center
        8: 'bottomRight'  // Bottom Right
    },

    // NEW: Get DVE style name
    getDVEStyleName: function(styleValue) {
        return this.dveStyleNames[styleValue] || `Style ${styleValue}`;
    },

    // CORRECTED: Get current DVE category based on style value - Push styles corrected to 24-31
    getCurrentDVECategory: function(styleValue) {
        if (styleValue === undefined || styleValue === null) return this.dveStyleCategories[0]; // Default to Push
        
        // Check Push styles (24-31)
        if (styleValue >= 24 && styleValue <= 31) {
            return this.dveStyleCategories[0]; // Push
        }
        
        // Check Squeeze styles (16-23)  
        if (styleValue >= 16 && styleValue <= 23) {
            return this.dveStyleCategories[1]; // Squeeze
        }
        
        // Default to Push for any other styles
        return this.dveStyleCategories[0];
    },

    // NEW: Get DVE style for grid position within a category
    getDVEStyleForGridPosition: function(category, gridIndex) {
        if (!category || !category.styles) {
            return null;
        }
        
        // Map grid positions to array indices (skip center position 4)
        let arrayIndex = gridIndex;
        if (gridIndex > 4) {
            arrayIndex = gridIndex - 1; // Adjust for skipped center position
        }
        
        if (arrayIndex >= category.styles.length) {
            return null;
        }
        
        return category.styles[arrayIndex];
    },

    hasStyleForGridPosition: function(category, gridIndex) {
        if (!category || !category.styles) {
            return false;
        }
        
        // Map grid positions to array indices (skip center position 4)
        let arrayIndex = gridIndex;
        if (gridIndex > 4) {
            arrayIndex = gridIndex - 1; // Adjust for skipped center position
        }
        
        return arrayIndex < category.styles.length;
    },

    // Resolve current display FPS from last-known ATEM state. Falls back
    // to 25 during the handshake window before the first state arrives.
    // State lives on Alpine's store, not on ATEMControl — `this.state` was
    // never assigned, so this used to always hit the fallback (giving
    // 25 fps display even on 1080p30/60 ATEMs).
    fps() {
        const store = (typeof Alpine !== 'undefined' && Alpine.store) ? Alpine.store('atem') : null;
        return (store && store.state && store.state.videoMode && store.state.videoMode.fps) || 25;
    },

    // Parse rate string to frames (for stinger time inputs)
    parseRate: function(rateStr) {
        try {
            if (typeof rateStr === 'number') return rateStr;
            const fps = this.fps();
            const parts = String(rateStr).split(':');
            if (parts.length === 2) {
                const minutes = parseInt(parts[0], 10);
                const frames = parseInt(parts[1], 10);
                if (isNaN(minutes) || isNaN(frames)) return fps;
                return Math.max(0, (minutes * fps) + frames);
            }
            return fps;
        } catch (e) {
            console.warn('Failed to parse rate:', rateStr);
            return this.fps();
        }
    },

    // Simplified command sender. Every payload is stamped with the active
    // M/E index — the dispatch table threads it into the
    // per-M/E pyatem ops (me defaults to 0 server-side when absent);
    // global verbs ignore it. Explicit params.me wins over the stamp.
    cmd(command, params = {}) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        const me = window.Alpine?.store?.('atem')?.activeMe ?? 0;
        this.ws.send(JSON.stringify({ command, me, ...params }));
    },

    // Audio per-strip command helper. Bundles ``source`` and ``channel``
    // from the strip object the template carries (the state's per-strip
    // dict carries both — UI just hands the strip back). Use for any
    // ``set_audio_strip_*`` verb.
    cmdAudio(command, strip, params = {}) {
        if (!strip) return;
        this.cmd(command, {
            source: strip.source,
            channel: strip.channel,
            ...params,
        });
    },

    // USK DVE shadow direction + altitude drag-pad helper. Two values
    // (angle 0..359°, altitude 10..100) drive the same cursor position
    // on a polar pad — atan2 around centre for angle, distance-from-
    // centre for altitude. Wired through the RealtimeSlider helper so
    // the dot follows the cursor in realtime (state mutated locally
    // on every move) AND the server gets a throttled stream of
    // commands (default 40 ms cadence). Release fires the canonical
    // wait-for-echo mute via dragRegistry, then sends a final
    // snapped-to-step value so the ATEM lands where the operator
    // released. Document-level mousemove/mouseup so the drag survives
    // the cursor leaving the pad — without that, fast drags lose the
    // dot mid-motion.
    USKLightDrag: {
        _drags: new Map(),  // uskIndex -> { moveHandler, upHandler }

        _compute(controlRect, clientX, clientY) {
            const centerX = controlRect.left + controlRect.width / 2;
            const centerY = controlRect.top + controlRect.height / 2;
            const radius = controlRect.width / 2;
            const maxDistance = radius - 10;
            const deltaX = clientX - centerX;
            const deltaY = clientY - centerY;
            let angle = Math.atan2(-deltaX, -deltaY) * 180 / Math.PI;
            if (angle < 0) angle += 360;
            const distance = Math.sqrt(deltaX * deltaX + deltaY * deltaY);
            const clampedDistance = Math.min(distance, maxDistance);
            const altitude = Math.max(10, Math.min(
                100, 100 - (clampedDistance / maxDistance) * 90));
            return { angle: Math.round(angle), altitude: Math.round(altitude) };
        },

        _applyLocal(uskIndex, angle, altitude) {
            const store = Alpine.store('atem');
            const d = store.state.mes[store.activeMe].usk?.data?.[uskIndex];
            if (d) {
                d.dve_light_direction = angle;
                d.dve_light_altitude = altitude;
            }
        },

        _sendDir(uskIndex, value) {
            Alpine.store('atem').send('set_usk_dve_light_direction',
                { key_index: uskIndex, direction: value });
        },

        _sendAlt(uskIndex, value) {
            Alpine.store('atem').send('set_usk_dve_light_altitude',
                { key_index: uskIndex, altitude: value });
        },

        startFromEvent(uskIndex, ev) {
            if (!ev.target.closest('.light-source')) return;
            ev.preventDefault();
            if (this._drags.has(uskIndex)) return;  // already dragging

            const activeMe = Alpine.store('atem').activeMe;
            const dirPath = `atem:me.${activeMe}.usk.${uskIndex}.dve_light_direction`;
            const altPath = `atem:me.${activeMe}.usk.${uskIndex}.dve_light_altitude`;
            window.ATEMControl.dragRegistry.startDrag(dirPath);
            window.ATEMControl.dragRegistry.startDrag(altPath);

            const controlEl = ev.currentTarget;
            const controlRect = controlEl.getBoundingClientRect();

            const moveHandler = (e) => {
                const { angle, altitude } = this._compute(
                    controlRect, e.clientX, e.clientY);
                this._applyLocal(uskIndex, angle, altitude);
                window.ATEMControl.RealtimeSlider.drag(
                    dirPath, angle, (v) => this._sendDir(uskIndex, v));
                window.ATEMControl.RealtimeSlider.drag(
                    altPath, altitude, (v) => this._sendAlt(uskIndex, v));
            };

            const upHandler = () => {
                document.removeEventListener('mousemove', moveHandler);
                document.removeEventListener('mouseup', upHandler);
                document.body.style.cursor = '';
                this._drags.delete(uskIndex);

                const store = Alpine.store('atem');
                const finalDir = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.dve_light_direction || 0;
                const finalAlt = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.dve_light_altitude ?? 25;
                window.ATEMControl.RealtimeSlider.release(
                    dirPath, finalDir,
                    (v) => this._sendDir(uskIndex, v),
                    { step: 1 });
                window.ATEMControl.RealtimeSlider.release(
                    altPath, finalAlt,
                    (v) => this._sendAlt(uskIndex, v),
                    { step: 1 });
            };

            document.addEventListener('mousemove', moveHandler);
            document.addEventListener('mouseup', upHandler);
            document.body.style.cursor = 'grabbing';
            this._drags.set(uskIndex, { moveHandler, upHandler });
        },

        clickFromEvent(uskIndex, ev) {
            if (ev.target.closest('.light-source')) return;
            // Single-click positions the dot to that point. No
            // realtime cadence needed since it's one discrete event.
            const { angle, altitude } = this._compute(
                ev.currentTarget.getBoundingClientRect(),
                ev.clientX, ev.clientY);
            this._applyLocal(uskIndex, angle, altitude);
            this._sendDir(uskIndex, angle);
            this._sendAlt(uskIndex, altitude);
        },
    },

    // Initialize system
    init() {
        console.log('🎛️ ATEM Control System Initializing');
        
        // Initialize T-Bar after DOM is ready
        setTimeout(() => this.initializeTBar(), 100);
        this.addButtonFeedback();

        // ASC-style momentary shift: holding the keyboard Shift key shows
        // the second source bank on the Program/Preview rows. Only active
        // when the source list is banked; ignored while typing in a field.
        document.addEventListener('keydown', (e) => {
            if (e.key !== 'Shift' || e.repeat) return;
            if (e.target?.matches?.('input, textarea, select')) return;
            const store = Alpine.store('atem');
            if (store?.busBanked()) store.busShiftHeld = true;
        });
        document.addEventListener('keyup', (e) => {
            if (e.key !== 'Shift') return;
            const store = Alpine.store('atem');
            if (store) store.busShiftHeld = false;
        });
        // Alt-tabbing away with Shift held would otherwise stick the bank.
        window.addEventListener('blur', () => {
            const store = Alpine.store('atem');
            if (store) store.busShiftHeld = false;
        });
        
        // Only auto-connect if coming from Connect page (has URL params)
        // This prevents auto-reconnect on page refresh or after disconnection
        const urlParams = new URLSearchParams(window.location.search);
        const urlIP = urlParams.get('ip');
        if (urlIP) {
            // User just came from Connect page, so auto-connect
            this.autoConnect();
        }
    },
    
    // Auto-connect logic - only called when coming from Connect page with URL params
    async autoConnect() {
        const urlParams = new URLSearchParams(window.location.search);
        const urlIP = urlParams.get('ip');
        const urlName = urlParams.get('name');
        
        // Only proceed if we have URL params (coming from Connect page)
        if (!urlIP) {
            return;
        }
        
        let savedIP = urlIP;
        let savedName = urlName;
        
        // Check if we're coming from Equipment List (has both IP and name)
        const isFromEquipmentList = urlIP && urlName;
        
        localStorage.setItem('atem_ip', urlIP);
        
        if (urlName) {
            localStorage.setItem('atem_name', urlName);
        }
        
        if (savedIP) {
            const alpineInstance = Alpine.store('atem');
            alpineInstance.currentIP = savedIP;
            
            // Only show name if coming from Equipment List
            if (isFromEquipmentList && savedName) {
                alpineInstance.currentName = savedName.trim();
            } else {
                // Not from Equipment List - lookup name from database
                const equipmentName = await this.lookupEquipmentName(savedIP);
                if (equipmentName) {
                    alpineInstance.currentName = equipmentName.trim();
                    localStorage.setItem('atem_name', equipmentName);
                } else {
                    alpineInstance.currentName = '';
                    localStorage.removeItem('atem_name');
                }
            }
            
            // Drop any stale ``atem_initial_state`` left over from the
            // Connect page. We used to apply it here as a "render
            // instantly" optimization, but with the
            // ``stateReady`` gate that's now actively harmful: applying
            // localStorage state flips stateReady true BEFORE this
            // page's WebSocket has had a chance to connect, which
            // briefly satisfies the disconnect-banner condition
            // (stateReady && !connected) for the ~500 ms gap until
            // connection_status arrives. The loading overlay handles
            // that window cleanly without the cached fallback.
            localStorage.removeItem('atem_initial_state');

            setTimeout(() => this.connect(savedIP), 500);
        }
    },
    
    // Lookup equipment name by IP from database
    async lookupEquipmentName(ipAddress) {
        try {
            const response = await fetch(`/atem/api/lookup-name/?ip=${encodeURIComponent(ipAddress)}`);
            const data = await response.json();
            
            if (data.found && data.name) {
                return data.name;
            }
            return null;
        } catch (error) {
            console.error('Error looking up equipment name:', error);
            return null;
        }
    },
    
    // Connection handling — the single WebSocket connect flow.
    //
    // ``options`` accepts the legacy boolean (isUserInitiated, used by the
    // recent-ATEMs dropdown) or an options object:
    //   userInitiated:  failures surface on this page instead of the
    //                   redirect-to-Connect flow
    //   updateUrl:      rewrite ?ip=/&name= via history.replaceState once
    //                   the connection succeeds (default true — both the
    //                   dropdown and quick-connect paths always did this)
    //   friendlyErrors: map raw server failure messages to the
    //                   operator-facing hints the header quick-connect
    //                   input has always shown
    // The header quick-connect input goes through Alpine's
    // $store.atem.quickConnect(), which is now a thin wrapper over this
    // (it used to carry its own duplicated ~165-line WS connect flow).
    async connect(ipAddress, options = false) {
        const opts = (typeof options === 'object' && options !== null)
            ? options
            : { userInitiated: !!options };
        const isUserInitiated = !!opts.userInitiated;
        const updateUrl = opts.updateUrl !== false;
        // Track if this is a user-initiated connection (like from quickConnect)
        this.isUserInitiatedConnection = isUserInitiated;
        this.connectFriendlyErrors = !!opts.friendlyErrors;
        
        // Only set reconnecting flag if there's an existing connection to close
        const hadExistingConnection = this.ws && this.ws.readyState !== WebSocket.CLOSED;
        
        if (hadExistingConnection) {
            // Set flag to indicate we're intentionally reconnecting
            this.isReconnecting = true;
            this.connectingIP = ipAddress;
            // Close old connection - its onclose handler will check isReconnecting flag
            this.ws.close();
            this.ws = null;
        } else {
            // No existing connection, so this is a fresh connection attempt
            this.isReconnecting = false;
            this.connectingIP = ipAddress;
        }
        
        const alpineInstance = Alpine.store('atem');
        alpineInstance.connected = false;
        alpineInstance.statusText = 'Connecting...';
        
        // Always show the IP we're connecting to (fixes wrong IP/name when switching via recent dropdown)
        alpineInstance.currentIP = ipAddress;
        localStorage.setItem('atem_ip', ipAddress);

        // Switching to a DIFFERENT ATEM: the store (and its media pool)
        // survives in-page switches, and the snapshot reconcile's
        // sticky-thumb rule would keep the previous ATEM's images on
        // screen while the new ATEM's thumbs are still null (observed
        // live 2026-07-02: ATEM A's pool shown while connected to B).
        // Start the pool empty; the first snapshot repopulates it at the
        // new ATEM's real topology. Same-IP reconnects keep their tiles.
        if (alpineInstance.mediaPool.forIP !== ipAddress) {
            alpineInstance.mediaPool.slots.splice(0);
            alpineInstance.mediaPool.players.splice(0);
            alpineInstance.mediaPool.locked = false;
            alpineInstance.mediaPool.forIP = ipAddress;

            // The HyperDeck transport modal (and its poll timers)
            // belongs to the OLD ATEM's decks — left alone, its transport
            // buttons keep driving the previous ATEM's deck. close() stops
            // the timers + hides the modal; also forget the deck list so a
            // reopen starts from the NEW ATEM's RXMS bindings.
            if (window.AtemHyperdeck) window.AtemHyperdeck.close();
            alpineInstance.hyperdeck.deckIp = null;
            alpineInstance.hyperdeck.decks = [];

            // Per-slot "Uploading…" overlays (and their safety
            // timers) belong to the old ATEM's pool.
            for (const slot of alpineInstance.pendingUploads.slice()) {
                alpineInstance._clearUploading(slot);
            }

            // The meter subscription died with the old consumer —
            // clear the flag so the audio panel's x-effect re-subscribes
            // on the new connection, and run the painted bars down to
            // silence instead of freezing at the last received level.
            alpineInstance.audioMetersSubscribed = false;
            window.AudioMeters.reset();

            // Cancel every in-flight realtime-slider send + drag/mute
            // guard — they reference the old ATEM's values.
            window.ATEMControl.RealtimeSlider.cancelAll();

            // A profile dialog opened against the old ATEM must not
            // apply its selections to the new one.
            if (window.AtemProfile && typeof window.AtemProfile.forceClose === 'function') {
                window.AtemProfile.forceClose();
            }

            // Gate the control surface behind the loading overlay
            // until the NEW ATEM's first atem_state arrives — otherwise the
            // old ATEM's full surface renders as the new one's. Only on an
            // in-page IP CHANGE: stateReady deliberately persists across
            // same-ATEM disconnect/reconnect (the redirect flow relies on
            // that — see updateState).
            alpineInstance.stateReady = false;
        }
        
        // Lookup name from database for the IP we're connecting to (single source of truth)
        const urlParams = new URLSearchParams(window.location.search);
        const urlIP = urlParams.get('ip');
        const urlName = urlParams.get('name');
        const isFromEquipmentList = urlIP && urlName;
        const isManualConnection = !urlIP || ipAddress !== urlIP;
        
        if (isManualConnection) {
            // Manual connection / switch - always set name from database for this IP
            const equipmentName = await this.lookupEquipmentName(ipAddress);
            if (equipmentName) {
                alpineInstance.currentName = equipmentName.trim();
                localStorage.setItem('atem_name', equipmentName);
            } else {
                alpineInstance.currentName = '';
                localStorage.removeItem('atem_name');
            }
        }
        
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/atem/`;
        
        // Remember which IP we're connecting to so we can update URL when connection succeeds (e.g. after switch)
        this.pendingConnectIP = updateUrl ? ipAddress : null;
        
        try {
            this.ws = new WebSocket(wsUrl);
            const currentWs = this.ws; // Capture reference for closure
            
            this.ws.onopen = () => {
                this.isReconnecting = false; // Connection successful, clear flag
                this.connectingIP = null;
                if (!this.connectFriendlyErrors) {
                    // Legacy behavior (kept as-is for the recent-ATEMs
                    // dropdown path): the flag is cleared as soon as the
                    // WebSocket opens, so a subsequent ATEM-side connect
                    // failure follows the redirect flow. The quick-connect
                    // path keeps the flag armed until the connect attempt
                    // actually resolves, so its failures stay on this page
                    // with an inline error — exactly what its private WS
                    // flow used to do.
                    this.isUserInitiatedConnection = false; // Connection successful, clear flag
                }
                this.cmd('connect', { ip_address: ipAddress });
            };
            
            this.ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                this.handleMessage(data);
            };
            
            this.ws.onclose = () => {
                const wasReconnecting = this.isReconnecting;
                // Check if this is the current WebSocket (the one we just created)
                const isCurrentSocket = this.ws === currentWs;
                
                // If we were reconnecting and this is the current socket closing, the new connection failed
                if (wasReconnecting && isCurrentSocket) {
                    // New connection failed - clear flags and redirect
                    this.isReconnecting = false;
                    this.connectingIP = null;
                } else if (!wasReconnecting) {
                    // Unexpected disconnection - clear flags
                    this.isReconnecting = false;
                    this.connectingIP = null;
                }
                
                alpineInstance.connected = false;
                if (this.connectFriendlyErrors && alpineInstance.statusText === 'Connecting...') {
                    // Quick-connect attempt whose socket closed before any
                    // connection_status arrived — the old quickConnect
                    // flow surfaced this as 'Connection failed' rather
                    // than 'Disconnected'.
                    alpineInstance.statusText = 'Connection failed';
                    this.connectFriendlyErrors = false;
                } else {
                    alpineInstance.statusText = 'Disconnected';
                }
                this.stopAllRateCountdowns();

                // Cancel EVERY in-flight realtime-slider send and
                // drag/mute guard (was: just the selected color generator's
                // three paths) — a dead socket can't deliver the echo the
                // mute windows are waiting for.
                window.ATEMControl.RealtimeSlider.cancelAll();

                // The server-side meter subscription died with the
                // socket. Clear the client flag so the audio panel's
                // x-effect re-subscribes once reconnected, and decay the
                // painted bars to silence — a frozen last-received level
                // reads as a live signal.
                alpineInstance.audioMetersSubscribed = false;
                window.AudioMeters.reset();

                // Only redirect if:
                // 1. This is the current socket closing (not an old one)
                // 2. It's NOT a user-initiated connection attempt (those should show error, not redirect)
                // 3. It's either an unexpected disconnection OR a failed reconnect
                const isUserInitiated = this.isUserInitiatedConnection;
                const shouldRedirect = isCurrentSocket && 
                                     !isUserInitiated && 
                                     (!wasReconnecting || (wasReconnecting && isCurrentSocket));
                
                if (shouldRedirect) {
                    window.location.href = '/atem/';
                } else if (isCurrentSocket && isUserInitiated) {
                    // User-initiated connection failed - clear flag and show error (don't redirect)
                    this.isUserInitiatedConnection = false;
                }
            };
            
            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                // Don't redirect here - let onclose handle it
                // onclose will check isReconnecting flag
            };
            
        } catch (error) {
            console.error('WebSocket connection error:', error);
            this.isReconnecting = false; // Clear flag since connection failed
            this.connectingIP = null;
            
            alpineInstance.connected = false;
            alpineInstance.statusText = 'Connection Failed';
            
            // If connection fails during setup, redirect to Connect page
            window.location.href = '/atem/';
        }
    },
    
    // Handle WebSocket messages
    handleMessage(data) {
        const alpineInstance = Alpine.store('atem');
        
        switch (data.type) {
            case 'connection_status':
                alpineInstance.connected = data.connected;
                alpineInstance.statusText = data.message;
                if (data.connected) {
                    this.isReconnecting = false; // Connection successful, clear flag
                    this.connectingIP = null;
                    this.isUserInitiatedConnection = false; // Connection successful, clear flag
                    this.connectFriendlyErrors = false;
                    // After switching ATEM, update URL to reflect current connection (name/IP already set in connect())
                    if (this.pendingConnectIP) {
                        const url = new URL(window.location);
                        url.searchParams.set('ip', this.pendingConnectIP);
                        if (alpineInstance.currentName) {
                            url.searchParams.set('name', alpineInstance.currentName);
                        } else {
                            url.searchParams.delete('name');
                        }
                        window.history.replaceState({}, '', url);
                        this.pendingConnectIP = null;
                    }
                    this.cmd('refresh');
                } else {
                    // Disconnected — redirect to the Connect page,
                    // stashing a contextual message in sessionStorage so
                    // the Connect page can surface it as an alert:
                    //   - Operator clicked the in-page Disconnect button:
                    //     no message (the redirect itself is the feedback)
                    //   - 5-minute inactivity timeout: server sets
                    //     ``auto_disconnected: true``
                    //   - ATEM lost mid-session (cable yank, power off):
                    //     server's _monitor_loop sends "ATEM connection
                    //     lost" via connection_status connected=false
                    //   - Outright connection failure (couldn't reach the
                    //     ATEM at all): also surfaced as "lost connection"
                    if (alpineInstance._userInitiatedDisconnect) {
                        // Manual disconnect — the disconnect() method on
                        // the store already kicked off the navigation.
                        // Make sure no stale message lingers.
                        try { sessionStorage.removeItem('atem_disconnect_reason'); } catch (e) {}
                    } else if (data.auto_disconnected) {
                        try {
                            sessionStorage.setItem(
                                'atem_disconnect_reason',
                                'Disconnected after 5 minutes of inactivity.');
                        } catch (e) {}
                        window.location.href = '/atem/';
                    } else if (this.isUserInitiatedConnection) {
                        // The connect attempt itself failed — let the
                        // user see the error on the connect page they
                        // were just on instead of force-redirecting.
                        this.isUserInitiatedConnection = false;
                        if (this.connectFriendlyErrors) {
                            // Quick-connect path: map raw server failure
                            // messages to the operator-facing hints the
                            // header input has always shown.
                            let errorMsg = data.message || 'Failed to connect to ATEM';
                            if (errorMsg.includes('timeout')) {
                                errorMsg = 'ATEM connection timeout - check IP address';
                            } else if (errorMsg.includes('refused') || errorMsg.includes('failed')) {
                                errorMsg = 'Cannot reach ATEM - check IP address and network';
                            }
                            alpineInstance.statusText = errorMsg;
                            this.connectFriendlyErrors = false;
                        }
                    } else if (!this.isReconnecting && !this.connectingIP) {
                        // Unexpected disconnect — ATEM dropped or
                        // initial connect never succeeded.
                        try {
                            sessionStorage.setItem(
                                'atem_disconnect_reason',
                                'Lost connection to ATEM.');
                        } catch (e) {}
                        window.location.href = '/atem/';
                    }
                }
                break;
            case 'atem_state':
                this.updateStateFromATEM(data.state);
                break;
            case 'tbar_cut':
                this.handleTBarCut();
                break;
            case 'media_pool_snapshot': {
                // In-place reconcile. Mutate mp.slots WITHOUT ever emptying
                // the array — emptying-then-refilling tears down every
                // <img> in the modal even with stable :key. Each slot is
                // only replaced if its content actually changed.
                //
                // Sticky-non-null thumb: if incoming.thumb is null but the
                // current slot has a thumb AND is still isUsed, keep the
                // existing thumb. Prevents the watcher's reconnect dump
                // (cache wiped, thumb=null until re-download) from blanking
                // tiles. A genuine clear (isUsed=false) still goes through.
                const mp = alpineInstance.mediaPool;
                const payload = data.data || {};
                if (Array.isArray(payload.slots)) {
                    alpineInstance._reconcileMediaPoolSlots(mp.slots, payload.slots);
                    for (const s of mp.slots) {
                        if (s.isUsed && s.thumb) alpineInstance._clearUploading(s.index);
                    }
                }
                if (Array.isArray(payload.players)) {
                    alpineInstance._reconcileMediaPoolPlayers(mp.players, payload.players);
                }
                if ('poolLocked' in payload) {
                    mp.locked = !!payload.poolLocked;
                }
                break;
            }
            case 'media_pool_lock': {
                // Watcher-detected foreign holder of the ATEM's media store
                // lock (or its release) — drives the modal banner.
                alpineInstance.mediaPool.locked = !!(data.data && data.data.locked);
                break;
            }
            case 'media_pool_slot_updated': {
                const slot = (data.data || {}).slot;
                if (slot && typeof slot.index === 'number') {
                    const mp = alpineInstance.mediaPool;
                    const pos = mp.slots.findIndex(s => s.index === slot.index);
                    if (pos >= 0) {
                        // Same sticky-non-null thumb rule as snapshot:
                        // when the watcher's MPfe handler emits a fresh
                        // hash with thumb=null (download not yet done),
                        // don't blank a tile we already have a thumb for.
                        const cur = mp.slots[pos];
                        const merged = (cur.thumb && slot.isUsed && !slot.thumb)
                            ? { ...slot, thumb: cur.thumb }
                            : slot;
                        if (alpineInstance._slotsDiffer(cur, merged)) {
                            mp.slots[pos] = merged;
                        }
                    }
                    // Upload completion signal: real image now on the slot.
                    if (slot.isUsed && slot.thumb) {
                        alpineInstance._clearUploading(slot.index);
                    }
                }
                break;
            }
            case 'media_pool_slot_uploaded': {
                // Fast-path: the upload webhook fired this with a thumb
                // synthesised from the source file — no watcher round-trip.
                // The event itself is the upload-completed signal, so the
                // Uploading overlay clears unconditionally even if the
                // server-side thumb encode failed (the watcher's eventual
                // slot_updated will fill in the image).
                const payload = data.data || {};
                const slotIdx = payload.slot;
                if (typeof slotIdx !== 'number') break;
                if (payload.thumb) {
                    const mp = alpineInstance.mediaPool;
                    const pos = mp.slots.findIndex(s => s.index === slotIdx);
                    if (pos >= 0) {
                        mp.slots[pos] = {
                            ...mp.slots[pos],
                            isUsed: true,
                            thumb: payload.thumb,
                            fileName: payload.fileName || mp.slots[pos].fileName || '',
                        };
                    }
                }
                alpineInstance._clearUploading(slotIdx);
                break;
            }
            case 'media_pool_upload_failed': {
                // The upload for this slot died server-side (connect / lock /
                // transfer failure reported via the uploader webhook). Clear
                // the Uploading overlay NOW and say so — same idiom as the
                // synchronous POST failure path in uploadSlot(). Before this,
                // the spinner silently sat out its 120s safety timer and a
                // 15s failure looked like a minute-long hang.
                const payload = data.data || {};
                const slotIdx = payload.slot;
                if (typeof slotIdx !== 'number') break;
                alpineInstance._clearUploading(slotIdx);
                alert(`Slot ${slotIdx + 1}: upload ${payload.status || 'failed'} — ` +
                      `the image was NOT changed on the ATEM. Please try again.`);
                break;
            }
            case 'media_pool_player_updated': {
                const player = (data.data || {}).player;
                if (player && typeof player.index === 'number') {
                    const mp = alpineInstance.mediaPool;
                    const pos = mp.players.findIndex(p => p.index === player.index);
                    if (pos >= 0) mp.players[pos] = player;
                }
                break;
            }
            case 'profile_capture_progress': {
                // Per-slot progress for an in-flight profile-save media
                // pool capture. The save dialog renders this as a
                // progress bar. Only honored if the session id matches
                // what runSave() generated — stale events from an
                // earlier save click on this same ATEM are discarded.
                const payload = data.data || {};
                if (alpineInstance.profileCaptureSession
                    && payload.session_id === alpineInstance.profileCaptureSession) {
                    alpineInstance.profileCaptureProgress = {
                        completed: payload.completed || 0,
                        total: payload.total || 0,
                        slot: payload.slot,
                        slot_name: payload.slot_name || '',
                        from_cache: !!payload.from_cache,
                    };
                }
                break;
            }
            case 'audio_meter_batch':
                // Server coalesces FMLv/FDLv into a single frame per
                // ~40ms tick. Iterate strips + master once per batch
                // instead of one WS message per strip per tick.
                if (Array.isArray(data.strips)) {
                    for (const stripPayload of data.strips) {
                        AudioMeters.paintStrip(stripPayload);
                    }
                }
                if (data.master) {
                    AudioMeters.paintMaster(data.master);
                }
                break;
            case 'error':
                console.error('ATEM Error:', data.message);
                break;
        }
    },
    
    // UPDATED: Update state from ATEM with individual rate countdown and T-Bar
    updateStateFromATEM(state) {
        if (!state) return;
        
        const alpineInstance = Alpine.store('atem');
        
        // Update T-Bar based on ATEM state
        this.updateTBarFromATEMState(state);
        
        // UPDATED: Handle rate countdowns including individual transition rates
        this.handleRateCountdowns(state);
        
        // Update Alpine state
        alpineInstance.updateState(state);
    },
    
    // UPDATED: Enhanced rate countdown system with individual transition rate support
    handleRateCountdowns(state) {
        // Per-M/E state arrives under state.mes[] (Stage 3C); the main-row
        // countdowns track the M/E this page is rendering.
        const me = state.mes?.[Alpine.store('atem').activeMe] || {};

        // Handle M/E transition countdown
        if (me.transition && me.transition.in_transition) {
            const framesRemaining = parseInt(me.transition.frames_remaining) || 0;
            const currentStyle = me.transition.style;
            
            // Start general M/E countdown
            if (!this.rateCountdowns.me.active) {
                this.startRateCountdown('me');
            }
            this.updateRateCountdown('me', framesRemaining);
            
            // UPDATED: Also start individual transition type countdown based on current style
            const transitionTypes = ['mix', 'dip', 'wipe', 'dve', 'stinger'];
            const currentTransitionType = transitionTypes[currentStyle];
            
            if (currentTransitionType && this.rateCountdowns[currentTransitionType]) {
                if (!this.rateCountdowns[currentTransitionType].active) {
                    this.startRateCountdown(currentTransitionType);
                }
                this.updateRateCountdown(currentTransitionType, framesRemaining);
            }
            
            // Stop other individual transition rate countdowns
            transitionTypes.forEach((type, index) => {
                if (index !== currentStyle && this.rateCountdowns[type] && this.rateCountdowns[type].active) {
                    this.stopRateCountdown(type);
                }
            });
            
        } else {
            // Stop M/E and all individual transition rate countdowns when transition is complete
            if (this.rateCountdowns.me.active) {
                this.stopRateCountdown('me');
            }
            
            // Stop all individual transition rate countdowns
            ['mix', 'dip', 'wipe', 'dve', 'stinger'].forEach(type => {
                if (this.rateCountdowns[type] && this.rateCountdowns[type].active) {
                    this.stopRateCountdown(type);
                }
            });
        }
        
        // Quick-panel per-DSK countdowns (dsk0/dsk1). Unlike TrPs/FtbS,
        // the ATEM does NOT stream frames_remaining for DSK transitions
        // (DskS only flags the edges; frames stays 0 throughout —
        // observed live 2026-07-31). So the countdown is synthesized
        // locally: the duration IS the known rate, ticked on a wall
        // clock until the falling edge arrives.
        (state.dsks || []).slice(0, 2).forEach((dsk, i) => {
            const key = 'dsk' + i;
            const cd = this.rateCountdowns[key];
            if (!cd) return;
            const transitioning = dsk && (dsk.in_transition || dsk.is_auto_transitioning);
            if (transitioning && !cd.active) {
                this.startRateCountdown(key);
                const startedAt = performance.now();
                const totalFrames = parseInt(dsk.rate) || 25;
                cd.syntheticTimer = setInterval(() => {
                    const elapsed = ((performance.now() - startedAt) / 1000) * this.fps();
                    this.updateRateCountdown(key, Math.max(0, Math.round(totalFrames - elapsed)));
                }, 100);
            } else if (!transitioning && cd.active) {
                clearInterval(cd.syntheticTimer);
                cd.syntheticTimer = null;
                this.stopRateCountdown(key);
            }
        });

        // Handle FTB countdown
        if (me.ftb && me.ftb.in_transition) {
            const framesRemaining = parseInt(me.ftb.frames_remaining) || 0;
            if (!this.rateCountdowns.ftb.active) {
                this.startRateCountdown('ftb');
            }
            this.updateRateCountdown('ftb', framesRemaining);
        } else {
            if (this.rateCountdowns.ftb.active) {
                this.stopRateCountdown('ftb');
            }
        }
    },

    startRateCountdown(transitionType) {
        const countdown = this.rateCountdowns[transitionType];
        if (!countdown) return;
        
        const rateInput = document.getElementById(countdown.inputId);
        if (rateInput) {
            countdown.originalValue = rateInput.value;
            countdown.active = true;
            rateInput.classList.add('rate-countdown-active');
        }
    },

    updateRateCountdown(transitionType, framesRemaining) {
        const countdown = this.rateCountdowns[transitionType];
        if (!countdown || !countdown.active) return;
        
        const rateInput = document.getElementById(countdown.inputId);
        if (rateInput) {
            const timeRemaining = this.formatFramesToTimeCode(framesRemaining);
            rateInput.value = timeRemaining;
            
            if (framesRemaining <= 10) {
                rateInput.classList.add('rate-countdown-ending');
            } else {
                rateInput.classList.remove('rate-countdown-ending');
            }
        }
    },

    stopRateCountdown(transitionType) {
        const countdown = this.rateCountdowns[transitionType];
        if (!countdown || !countdown.active) return;
        
        const rateInput = document.getElementById(countdown.inputId);
        if (rateInput && countdown.originalValue) {
            rateInput.value = countdown.originalValue;
            rateInput.classList.remove('rate-countdown-active', 'rate-countdown-ending');
            countdown.originalValue = null;
            countdown.active = false;
        }
    },

    stopAllRateCountdowns() {
        Object.keys(this.rateCountdowns).forEach(type => {
            this.stopRateCountdown(type);
        });
    },

    formatFramesToTimeCode(frames) {
        if (!frames || frames < 0) return "0:00";
        const fps = this.fps();
        const totalFrames = Math.round(frames);
        const seconds = Math.floor(totalFrames / fps);
        const remainingFrames = totalFrames % fps;
        return `${seconds}:${remainingFrames.toString().padStart(2, '0')}`;
    },
    
    // RESTORED: T-Bar System
    initializeTBar() {
        this.tbarElements = {
            track: document.getElementById('tbarTrack'),
            handle: document.getElementById('tbarHandle'),
            progress: document.getElementById('tbarProgress'),
            positionDisplay: document.getElementById('tbarPositionDisplay')
        };
        
        if (!this.tbarElements.track || !this.tbarElements.handle) {
            console.warn('T-Bar elements not found');
            return;
        }
        
        this.updateTBarVisual();
        this.setupTBarInteraction();
        
        console.log('🎛️ Vertical T-Bar system initialized');
    },

    setupTBarInteraction() {
        const track = this.tbarElements.track;
        const handle = this.tbarElements.handle;
        
        if (!track || !handle) return;
        
        let startY = 0;
        let startPosition = 0;
        
        const startDrag = (e) => {
            if (this.tbarState.isAnimating) return;
            
            e.preventDefault();
            this.tbarState.isDragging = true;
            
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            startY = clientY;
            startPosition = this.tbarState.position;
            
            document.addEventListener('mousemove', handleDrag);
            document.addEventListener('mouseup', endDrag);
            document.addEventListener('touchmove', handleDrag, { passive: false });
            document.addEventListener('touchend', endDrag);
            
            handle.classList.add('tbar-dragging');
        };
        
        const handleDrag = (e) => {
            if (!this.tbarState.isDragging) return;
            
            e.preventDefault();
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            const deltaY = clientY - startY;
            const trackHeight = track.offsetHeight - handle.offsetHeight;
            const deltaPercent = (deltaY / trackHeight) * 100;
            
            const newPosition = Math.max(0, Math.min(100, startPosition + deltaPercent));
            this.setTBarPosition(newPosition);
        };
        
        const endDrag = () => {
            if (!this.tbarState.isDragging) return;
            
            this.tbarState.isDragging = false;
            
            document.removeEventListener('mousemove', handleDrag);
            document.removeEventListener('mouseup', endDrag);
            document.removeEventListener('touchmove', handleDrag);
            document.removeEventListener('touchend', endDrag);
            
            handle.classList.remove('tbar-dragging');
            this.snapTBarToState();
        };
        
        const handleTrackClick = (e) => {
            if (this.tbarState.isAnimating || this.tbarState.isDragging) return;
            if (e.target === handle) return;
            
            const rect = track.getBoundingClientRect();
            const clientY = e.touches ? e.touches[0].clientY : e.clientY;
            const clickY = clientY - rect.top;
            const trackHeight = track.offsetHeight;
            const clickPercent = (clickY / trackHeight) * 100;
            
            if (clickPercent < 50) {
                if (this.tbarState.position > 50) {
                    this.cmd('auto');
                }
            } else {
                if (this.tbarState.position < 50) {
                    this.cmd('auto');
                }
            }
        };
        
        handle.addEventListener('mousedown', startDrag);
        track.addEventListener('mousedown', handleTrackClick);
        handle.addEventListener('touchstart', startDrag, { passive: false });
        track.addEventListener('touchstart', handleTrackClick, { passive: false });
    },

    setTBarPosition(position, animate = false) {
        position = Math.max(0, Math.min(100, position));
        this.tbarState.position = position;
        
        if (animate) {
            this.tbarElements.handle.style.transition = 'top 0.1s ease-out';
        } else {
            this.tbarElements.handle.style.transition = 'none';
        }
        
        this.updateTBarVisual();
    },

    updateTBarVisual() {
        const handle = this.tbarElements.handle;
        const positionDisplay = this.tbarElements.positionDisplay;
        
        if (!handle) return;
        
        handle.style.top = `${this.tbarState.position}%`;
        
        if (positionDisplay) {
            positionDisplay.textContent = `Position: ${Math.round(this.tbarState.position)}%`;
        }
    },

    updateTBarFromATEMState(state) {
        if (!state || this.tbarState.isDragging) return;

        const me = state.mes?.[Alpine.store('atem').activeMe] || {};
        if (me.transition && me.transition.in_transition) {
            const framesRemaining = parseInt(me.transition.frames_remaining) || 0;
            const rate = this.parseRate(me.transition.rate) || this.fps();
            
            if (!this.tbarState.isAnimating) {
                this.animateTBarForAuto();
            }
            
            this.updateTBarProgress(framesRemaining, rate);
        } else {
            if (this.tbarState.isAnimating) {
                this.completeTBarTransition();
            }
        }
    },

    animateTBarForAuto() {
        this.tbarState.isAnimating = true;
        this.tbarState.targetPosition = this.tbarState.position < 50 ? 100 : 0;
        this.tbarElements.handle.style.transition = 'top 0.05s linear';
    },

    updateTBarProgress(framesRemaining, totalFrames) {
        if (!this.tbarState.isAnimating) return;
        
        const progress = Math.max(0, Math.min(1, (totalFrames - framesRemaining) / totalFrames));
        const startPos = this.tbarState.targetPosition === 100 ? 0 : 100;
        const endPos = this.tbarState.targetPosition;
        const currentPos = startPos + (endPos - startPos) * progress;
        
        this.setTBarPosition(currentPos);
        
        if (this.tbarElements.progress) {
            this.tbarElements.progress.style.height = `${progress * 100}%`;
            this.tbarElements.progress.style.opacity = '0.3';
        }
    },

    completeTBarTransition() {
        this.tbarState.isAnimating = false;
        this.setTBarPosition(this.tbarState.targetPosition);
        
        if (this.tbarElements.progress) {
            this.tbarElements.progress.style.opacity = '0';
        }
        
        setTimeout(() => {
            if (this.tbarElements.handle) {
                this.tbarElements.handle.style.transition = 'none';
            }
        }, 100);
    },

    handleTBarCut() {
        this.tbarState.targetPosition = this.tbarState.position < 50 ? 100 : 0;
        this.setTBarPosition(this.tbarState.targetPosition);
    },

    snapTBarToState() {
        const shouldBeBottom = this.tbarState.position > 50;
        this.tbarState.targetPosition = shouldBeBottom ? 100 : 0;
        
        this.tbarElements.handle.style.transition = 'top 0.3s ease-out';
        this.setTBarPosition(this.tbarState.targetPosition);
        
        setTimeout(() => {
            if (this.tbarElements.handle) {
                this.tbarElements.handle.style.transition = 'none';
            }
        }, 300);
    },
    
    // Button feedback
    addButtonFeedback() {
        document.addEventListener('click', function(e) {
            if (e.target.classList.contains('btn')) {
                e.target.style.transform = 'scale(0.96)';
                setTimeout(() => {
                    e.target.style.transform = '';
                }, 80);
            }
        });
    }
}; // END of ATEMControl object

// Chroma Sample Box Alpine Component - DEFINED OUTSIDE ATEMControl
function chromaSampleBox(uskIndex) {
    return {
        isDragging: false,
        startX: 0,
        startY: 0,
        startCursorX: 0,
        startCursorY: 0,
        
        get cursorStyle() {
            const store = Alpine.store('atem');
            const x = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_x || 0.5) * 100;
            const y = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_y || 0.5) * 100;
            const size = (store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_size || 0.1) * 100;
            
            return `
                left: ${x}%;
                top: ${y}%;
                width: ${size}%;
                height: ${size}%;
                transform: translate(-50%, -50%);
            `;
        },
        
        startDrag(e) {
            e.preventDefault();
            this.isDragging = true;
            
            const store = Alpine.store('atem');
            const rect = e.currentTarget.parentElement.getBoundingClientRect();
            
            // Get starting positions
            this.startX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            this.startY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            this.startCursorX = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_x || 0.5;
            this.startCursorY = store.state.mes[store.activeMe].usk?.data?.[uskIndex]?.chroma_sample_cursor_y || 0.5;
            
            // Add event listeners
            const moveHandler = (e) => this.handleDrag(e, rect);
            const endHandler = () => this.endDrag(moveHandler, endHandler);
            
            document.addEventListener('mousemove', moveHandler);
            document.addEventListener('mouseup', endHandler);
            document.addEventListener('touchmove', moveHandler, { passive: false });
            document.addEventListener('touchend', endHandler);
        },
        
        handleDrag(e, rect) {
            if (!this.isDragging) return;
            e.preventDefault();
            
            const store = Alpine.store('atem');
            const clientX = e.type.includes('touch') ? e.touches[0].clientX : e.clientX;
            const clientY = e.type.includes('touch') ? e.touches[0].clientY : e.clientY;
            
            // Calculate delta
            const deltaX = (clientX - this.startX) / rect.width;
            const deltaY = (clientY - this.startY) / rect.height;
            
            // Update position
            const newX = Math.max(0, Math.min(1, this.startCursorX + deltaX));
            const newY = Math.max(0, Math.min(1, this.startCursorY + deltaY));
            
            store.setUSKChromaSampleCursorX(uskIndex, newX);
            store.setUSKChromaSampleCursorY(uskIndex, newY);
        },
        
        endDrag(moveHandler, endHandler) {
            this.isDragging = false;
            document.removeEventListener('mousemove', moveHandler);
            document.removeEventListener('mouseup', endHandler);
            document.removeEventListener('touchmove', moveHandler);
            document.removeEventListener('touchend', endHandler);
        }
    };
}

// Make chromaSampleBox globally available for Alpine
window.chromaSampleBox = chromaSampleBox;


// ===========================================================================
// Realtime-slider drag-handler factory
// ===========================================================================
// Every continuous slider on the control surface drives the ATEM through
// the same method quadruplet, bound from the templates by name:
//
//   set<Name>(..., value)      fire-and-forget single send — number inputs
//                              and +/- spinner buttons (omitted for the
//                              stinger / DVE-transition params, which never
//                              had one)
//   start<Name>Drag(...)       arms the dragRegistry guard for the path
//   drag<Name>(..., value)     optimistic state write + throttled send
//   release<Name>(..., value)  final state write + flush, with snap-to-step
//                              and the post-release wait-for-echo mute
//
// These were 31 hand-copied quadruplets (~1,000 lines); they are now
// generated from the descriptor tables below. Each row reproduces its
// original handler's exact parameters: dragRegistry path, command verb,
// payload key, and release step.
//
// SNAP-BACK WARNING: the dragRegistry path built here must stay in sync
// with the per-field guard lists in updateState() (the localUskLuma /
// localUskPattern / localUskDVE / localUskChroma / localDSK / localStinger
// / localDVETx captures). updateState restores the operator's local value
// while dragRegistry.shouldSkip() says the echo hasn't landed; a path the
// guard lists don't cover reintroduces the snap-back-on-release failure
// mode. When adding a row here, add the field to the matching guard list.

// USK-indexed sliders. dragRegistry path:
// `atem:me.${activeMe}.usk.${uskIndex}.<field>` (M/E-indexed since Stage
// 4A so an M/E switch can't cross-wire echo guards); state target:
// state.mes[activeMe].usk.data[uskIndex].<field>; wire payload:
// { key_index: uskIndex, <payloadKey>: value } — cmd() stamps the active
// M/E index onto every payload. Wire-scale notes carried
// over from the hand-written handlers: luma + pattern clip/gain/size are
// u16 x10..x100 of percent with a wire grid finer than the UI step, so
// the release tolerance defaults to the step; advanced-chroma params use
// step 0.001 to match the sliders' HTML step attribute.
const USK_SLIDER_DESCRIPTORS = [
    { name: 'USKLumaClip',                field: 'luma_clip',                 verb: 'set_usk_luma_clip',                 payloadKey: 'clip',             step: 0.1 },
    { name: 'USKLumaGain',                field: 'luma_gain',                 verb: 'set_usk_luma_gain',                 payloadKey: 'gain',             step: 0.1 },
    { name: 'USKPatternSize',             field: 'pattern_size',              verb: 'set_usk_pattern_size',              payloadKey: 'size',             step: 0.1 },
    { name: 'USKPatternSymmetry',         field: 'pattern_symmetry',          verb: 'set_usk_pattern_symmetry',          payloadKey: 'symmetry',         step: 0.1 },
    { name: 'USKPatternSoftness',         field: 'pattern_softness',          verb: 'set_usk_pattern_softness',          payloadKey: 'softness',         step: 0.1 },
    { name: 'USKDVERotation',             field: 'dve_rotation',              verb: 'set_usk_dve_rotation',              payloadKey: 'rotation',         step: 1 },
    { name: 'USKDVEBorderHue',            field: 'dve_border_hue',            verb: 'set_usk_dve_border_hue',            payloadKey: 'hue',              step: 0.1 },
    { name: 'USKDVEBorderSaturation',     field: 'dve_border_saturation',     verb: 'set_usk_dve_border_saturation',     payloadKey: 'saturation',       step: 0.1 },
    { name: 'USKDVEBorderLuma',           field: 'dve_border_luma',           verb: 'set_usk_dve_border_luma',           payloadKey: 'luma',             step: 0.1 },
    { name: 'USKDVEBorderOuterWidth',     field: 'dve_border_outer_width',    verb: 'set_usk_dve_border_outer_width',    payloadKey: 'outerWidth',       step: 0.01 },
    { name: 'USKDVEBorderInnerWidth',     field: 'dve_border_inner_width',    verb: 'set_usk_dve_border_inner_width',    payloadKey: 'innerWidth',       step: 0.01 },
    { name: 'USKDVEBorderOuterSoftness',  field: 'dve_border_outer_softness', verb: 'set_usk_dve_border_outer_softness', payloadKey: 'outerSoftness',    step: 1 },
    { name: 'USKDVEBorderInnerSoftness',  field: 'dve_border_inner_softness', verb: 'set_usk_dve_border_inner_softness', payloadKey: 'innerSoftness',    step: 1 },
    { name: 'USKDVEBorderOpacity',        field: 'dve_border_opacity',        verb: 'set_usk_dve_border_opacity',        payloadKey: 'opacity',          step: 1 },
    { name: 'USKChromaForeground',        field: 'chroma_foreground',         verb: 'set_usk_chroma_foreground',         payloadKey: 'foreground',       step: 0.001 },
    { name: 'USKChromaBackground',        field: 'chroma_background',         verb: 'set_usk_chroma_background',         payloadKey: 'background',       step: 0.001 },
    { name: 'USKChromaKeyEdge',           field: 'chroma_key_edge',           verb: 'set_usk_chroma_key_edge',           payloadKey: 'keyEdge',          step: 0.001 },
    { name: 'USKChromaSpill',             field: 'chroma_spill',              verb: 'set_usk_chroma_spill',              payloadKey: 'spill',            step: 0.001 },
    { name: 'USKChromaFlareSuppression',  field: 'chroma_flare_suppression',  verb: 'set_usk_chroma_flare_suppression',  payloadKey: 'flareSuppression', step: 0.001 },
    { name: 'USKChromaBrightness',        field: 'chroma_brightness',         verb: 'set_usk_chroma_brightness',         payloadKey: 'brightness',       step: 0.001 },
    { name: 'USKChromaContrast',          field: 'chroma_contrast',           verb: 'set_usk_chroma_contrast',           payloadKey: 'contrast',         step: 0.001 },
    { name: 'USKChromaSaturation',        field: 'chroma_saturation',         verb: 'set_usk_chroma_saturation',         payloadKey: 'saturation',       step: 0.001 },
    { name: 'USKChromaRed',               field: 'chroma_red',                verb: 'set_usk_chroma_red',                payloadKey: 'red',              step: 0.001 },
    { name: 'USKChromaGreen',             field: 'chroma_green',              verb: 'set_usk_chroma_green',              payloadKey: 'green',            step: 0.001 },
    { name: 'USKChromaBlue',              field: 'chroma_blue',               verb: 'set_usk_chroma_blue',               payloadKey: 'blue',             step: 0.001 },
];

// DSK-indexed sliders (the settings DSK sections render via
// x-for off topology.dsks). dragRegistry path: `atem:dsk.${dskIndex}.<field>`;
// state target: state.dsks[dskIndex].<field>; wire payload:
// { dsk: dskIndex, <payloadKey>: value }. Same DSK clip/gain wire scale
// as before the restructure (u16 x10 of percent).
const DSK_SLIDER_DESCRIPTORS = [
    { name: 'DSKClip', field: 'clip', verb: 'set_dsk_clip', payloadKey: 'clip', step: 0.1 },
    { name: 'DSKGain', field: 'gain', verb: 'set_dsk_gain', payloadKey: 'gain', step: 0.1 },
];

// Non-indexed sliders (stinger, DVE-transition). `container` resolves
// the state object the field lives on — it mirrors the original handlers'
// optional-chained existence check, so a missing sub-object means "skip the
// optimistic write, still send". `setter: false` rows never had a
// fire-and-forget set<Name>() (stinger / DVE-tx are slider-only), so none
// is generated. Stinger and DVE-tx share the USK-luma wire scale (x10 of
// percent); DVE-tx historically used the wrong scale and was corrected.
// ``path`` is a function of the store — these transition
// params are per-M/E, so the registry key embeds the active M/E index.
const GLOBAL_SLIDER_DESCRIPTORS = [
    { name: 'StingerClip', field: 'clip', verb: 'set_stinger_clip', payloadKey: 'clip', step: 0.1, setter: false,
      path: (store) => `atem:me.${store.activeMe}.transition.stinger.clip`, container: (store) => store.state.mes[store.activeMe].transition?.stinger },
    { name: 'StingerGain', field: 'gain', verb: 'set_stinger_gain', payloadKey: 'gain', step: 0.1, setter: false,
      path: (store) => `atem:me.${store.activeMe}.transition.stinger.gain`, container: (store) => store.state.mes[store.activeMe].transition?.stinger },
    { name: 'DVEClip',     field: 'clip', verb: 'set_dve_clip',     payloadKey: 'clip', step: 0.1, setter: false,
      path: (store) => `atem:me.${store.activeMe}.transition.dve.clip`,     container: (store) => store.state.mes[store.activeMe].transition?.dve },
    { name: 'DVEGain',     field: 'gain', verb: 'set_dve_gain',     payloadKey: 'gain', step: 0.1, setter: false,
      path: (store) => `atem:me.${store.activeMe}.transition.dve.gain`,     container: (store) => store.state.mes[store.activeMe].transition?.dve },
];

// Builds the handler methods spread into the Alpine store literal below.
// All generated functions are regular (non-arrow) so `this` binds to the
// store when Alpine invokes them as methods.
function buildSliderHandlers() {
    const handlers = {};

    for (const d of USK_SLIDER_DESCRIPTORS) {
        // M/E-indexed registry path so switching M/Es can't
        // collide echo guards across M/Es. Must stay in sync with the
        // guard-side strings in updateState.
        const path = (store, i) => `atem:me.${store.activeMe}.usk.${i}.${d.field}`;
        const writeState = function (uskIndex, parsed) {
            if (this.state.mes[this.activeMe].usk?.data?.[uskIndex]) {
                this.state.mes[this.activeMe].usk.data[uskIndex][d.field] = parsed;
            }
        };
        const sendValue = function (uskIndex, v) {
            this.send(d.verb, { key_index: uskIndex, [d.payloadKey]: v });
        };
        handlers[`set${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            sendValue.call(this, uskIndex, parsed);
        };
        handlers[`start${d.name}Drag`] = function (uskIndex) {
            window.ATEMControl.dragRegistry.startDrag(path(this, uskIndex));
        };
        handlers[`drag${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            window.ATEMControl.RealtimeSlider.drag(path(this, uskIndex), parsed, (v) => {
                sendValue.call(this, uskIndex, v);
            });
        };
        handlers[`release${d.name}`] = function (uskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, uskIndex, parsed);
            window.ATEMControl.RealtimeSlider.release(path(this, uskIndex), parsed, (v) => {
                sendValue.call(this, uskIndex, v);
            }, { step: d.step });
        };
    }

    for (const d of DSK_SLIDER_DESCRIPTORS) {
        const path = (i) => `atem:dsk.${i}.${d.field}`;
        const writeState = function (dskIndex, parsed) {
            if (this.state.dsks?.[dskIndex]) {
                this.state.dsks[dskIndex][d.field] = parsed;
            }
        };
        const sendValue = function (dskIndex, v) {
            this.send(d.verb, { dsk: dskIndex, [d.payloadKey]: v });
        };
        handlers[`set${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            sendValue.call(this, dskIndex, parsed);
        };
        handlers[`start${d.name}Drag`] = function (dskIndex) {
            window.ATEMControl.dragRegistry.startDrag(path(dskIndex));
        };
        handlers[`drag${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            window.ATEMControl.RealtimeSlider.drag(path(dskIndex), parsed, (v) => {
                sendValue.call(this, dskIndex, v);
            });
        };
        handlers[`release${d.name}`] = function (dskIndex, value) {
            const parsed = parseFloat(value);
            writeState.call(this, dskIndex, parsed);
            window.ATEMControl.RealtimeSlider.release(path(dskIndex), parsed, (v) => {
                sendValue.call(this, dskIndex, v);
            }, { step: d.step });
        };
    }

    for (const d of GLOBAL_SLIDER_DESCRIPTORS) {
        const writeState = function (parsed) {
            const target = d.container(this);
            if (target) {
                target[d.field] = parsed;
            }
        };
        const sendValue = function (v) {
            this.send(d.verb, { [d.payloadKey]: v });
        };
        if (d.setter) {
            handlers[`set${d.name}`] = function (value) {
                const parsed = parseFloat(value);
                writeState.call(this, parsed);
                sendValue.call(this, parsed);
            };
        }
        handlers[`start${d.name}Drag`] = function () {
            window.ATEMControl.dragRegistry.startDrag(d.path(this));
        };
        handlers[`drag${d.name}`] = function (value) {
            const parsed = parseFloat(value);
            writeState.call(this, parsed);
            window.ATEMControl.RealtimeSlider.drag(d.path(this), parsed, (v) => {
                sendValue.call(this, v);
            });
        };
        handlers[`release${d.name}`] = function (value) {
            const parsed = parseFloat(value);
            writeState.call(this, parsed);
            window.ATEMControl.RealtimeSlider.release(d.path(this), parsed, (v) => {
                sendValue.call(this, v);
            }, { step: d.step });
        };
    }

    return handlers;
}

// UPDATED: Alpine.js Global Store with Individual Transition Rate Support and DVE State
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
        // and bindings update wholesale.
        stateReady: false,
        // Which M/E this page renders. All per-M/E reads route through
        // state.mes[activeMe]; the command send path stamps
        // every payload with it (see ATEMControl.cmd), and the
        // dragRegistry paths embed it. Switch via setActiveMe(), never by
        // assigning directly — the rate inputs need a resync on switch.
        activeMe: 0,
        // Set true when the operator clicks the in-page Disconnect button.
        // ``connection_status`` then knows to redirect WITHOUT stashing a
        // "lost connection" message on the Connect page.
        _userInitiatedDisconnect: false,
        currentIP: '---.---.---.---',
        currentName: '',
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

        // Media pool — pushed from server via Channels group, real-time multi-user.
        // Starts empty; the first snapshot from the server populates arrays at
        // the ATEM's actual topology (e.g. 20 slots / 2 players on a TVS HD).
        mediaPool: {
            slots: [],
            players: [],
            // True while the ATEM refuses to grant the media store lock
            // (another client — automation software, a stuck session —
            // holds it). Drives the modal's honest banner instead of
            // anonymous forever-spinners.
            locked: false,
            // Which ATEM the slots/players below belong to. The store
            // survives in-page ATEM switches (recent dropdown / quick
            // connect); connect() resets the pool when this doesn't match
            // the target IP so ATEM A's images can never masquerade as
            // ATEM B's (the snapshot reconcile's sticky-thumb rule would
            // otherwise preserve them when B's thumbs are still null).
            forIP: null,
            get stats() {
                let loaded = 0, used = 0;
                for (const s of this.slots) {
                    if (s.isUsed) used++;
                    if (s.isUsed && s.thumb) loaded++;
                }
                return { loaded, used };
            },
            get playerSlotSet() {
                const set = new Set();
                for (const p of this.players) {
                    if (p.type === 'still') set.add(p.stillIndex);
                }
                return set;
            },
            slotPlayers(idx) {
                const out = [];
                for (const p of this.players) {
                    if (p.type === 'still' && p.stillIndex === idx) out.push(p.index);
                }
                return out;
            }
        },

        // Tally: is a media player currently contributing to the program output?
        // True if its fill or key source is on ANY M/E's PGM directly, or is
        // the fill/key of any on-air USK on ANY M/E, or of an on-air DSK
        // (global). All M/Es are scanned — not just the active one — because
        // a background M/E's output can be routed downstream and the slot
        // delete/replace confirmations gate off this tally; on a 1-M/E
        // switcher the loop is mes[0] only, identical to the old activeMe
        // scan. (isSlotOnAir inherits the all-M/E scan via this helper.)
        // MP source IDs follow the BMD convention: MP1=3010/3011, MP2=3020/3021,
        // MP3=3030/3031, MP4=3040/3041.
        isMediaPlayerOnAir(playerIndex) {
            const state = this.state;
            if (!state) return false;
            const fill = 3010 + playerIndex * 10;
            const key = fill + 1;
            const hits = (src) => src === fill || src === key;

            for (const me of state.mes || []) {
                if (!me) continue;
                if (hits(me.program)) return true;

                const uskStates = (me.usk && me.usk.states) || [];
                const uskData = (me.usk && me.usk.data) || [];
                for (let i = 0; i < uskStates.length; i++) {
                    if (!uskStates[i]) continue;
                    const d = uskData[i] || {};
                    if (hits(d.fill_source) || hits(d.key_source)) return true;
                }
            }

            for (const dsk of state.dsks || []) {
                if (dsk && dsk.on_air && (hits(dsk.fill_source) || hits(dsk.key_source))) return true;
            }

            return false;
        },

        // Lookup helper for the delete dialog / anywhere else that has a
        // slot index but needs the full slot entry.
        getSlot(slotIndex) {
            if (slotIndex === null || slotIndex === undefined) return null;
            return this.mediaPool.slots.find(s => s.index === slotIndex) || null;
        },

        // --- Upload (drag-drop onto a slot) ---
        // Track slots currently awaiting a new thumbnail from an upload we
        // initiated. Kept as an array so Alpine reactivity fires cleanly.
        pendingUploads: [],

        isSlotUploading(slotIndex) {
            return this.pendingUploads.includes(slotIndex);
        },

        _markUploading(slotIndex) {
            if (!this.pendingUploads.includes(slotIndex)) {
                this.pendingUploads.push(slotIndex);
            }
        },

        _clearUploading(slotIndex) {
            // Retire the slot's safety timer with the overlay —
            // otherwise a previous upload's timer fires 120 s later and
            // kills the overlay of a rapid re-upload to the same slot.
            const t = this._uploadSafetyTimers[slotIndex];
            if (t) {
                clearTimeout(t);
                delete this._uploadSafetyTimers[slotIndex];
            }
            const i = this.pendingUploads.indexOf(slotIndex);
            if (i >= 0) this.pendingUploads.splice(i, 1);
        },

        // Per-slot upload safety-timer handles. Plain non-reactive
        // property — nothing binds to it; only _armUploadSafetyTimer /
        // _clearUploading touch it.
        _uploadSafetyTimers: {},

        _armUploadSafetyTimer(slotIndex) {
            const prev = this._uploadSafetyTimers[slotIndex];
            if (prev) clearTimeout(prev);
            this._uploadSafetyTimers[slotIndex] = setTimeout(() => {
                console.warn('[mediaPoolUpload] safety timeout fired for slot', slotIndex);
                this._clearUploading(slotIndex);
            }, 120000);
        },

        // Optimistic clear after a delete confirm. The frontend already
        // knows which slot was cleared, so update the tile immediately
        // rather than waiting for the watcher's slot_updated round-trip
        // (which the snapshot reconcile's sticky-thumb rule would also
        // mask if the watcher reconnects with the cleared state). The
        // eventual authoritative slot_updated either matches what we
        // just set (no-op via _slotsDiffer) or corrects it.
        optimisticClearSlot(slotIndex) {
            const pos = this.mediaPool.slots.findIndex(s => s.index === slotIndex);
            if (pos < 0) return;
            this.mediaPool.slots[pos] = {
                ...this.mediaPool.slots[pos],
                isUsed: false,
                thumb: null,
                hash: '',
                fileName: '',
            };
        },

        // Optimistic player retarget after the user drags a slot onto a
        // player tile. The watcher can miss the MPCE event from ATEM if
        // it's still mid-reconnect from a recent upload-lockout, leaving
        // the player tile stale. The frontend already knows the new
        // pairing — apply it immediately. The eventual authoritative
        // player_updated either matches (no-op) or corrects it.
        optimisticSetPlayerStill(playerIndex, slotIndex) {
            const pos = this.mediaPool.players.findIndex(p => p.index === playerIndex);
            if (pos < 0) return;
            this.mediaPool.players[pos] = {
                ...this.mediaPool.players[pos],
                type: 'still',
                stillIndex: slotIndex,
            };
        },

        // Equality check used by both reconcilers. Compare every user-
        // visible field — if all match, no DOM update is needed.
        _slotsDiffer(a, b) {
            return a.index !== b.index
                || a.isUsed !== b.isUsed
                || a.hash !== b.hash
                || a.fileName !== b.fileName
                || a.thumb !== b.thumb;
        },

        _playersDiffer(a, b) {
            return a.index !== b.index
                || a.type !== b.type
                || a.stillIndex !== b.stillIndex
                || a.clipIndex !== b.clipIndex;
        },

        // In-place array reconcile — never clears the array length, so
        // <template x-for> with stable :key reuses every DOM node it can.
        // Slots whose content is unchanged are left strictly alone.
        _reconcileMediaPoolSlots(target, incoming) {
            const incomingByIdx = new Map(incoming.map(s => [s.index, s]));
            // Update / replace existing slots in place
            for (let i = 0; i < target.length; i++) {
                const cur = target[i];
                const inc = incomingByIdx.get(cur.index);
                if (!inc) continue;
                // Snapshot sticky-slot: if we have a thumb and the incoming
                // doesn't, keep the cur slot ENTIRELY unchanged — not just
                // the thumb. Snapshots arrive in transient empty states
                // when the watcher reconnects (cache cleared then re-
                // populated by streaming MPfe events). The template uses
                // `x-if="slot.isUsed && slot.thumb"` to gate the <img>
                // element; if isUsed flips off briefly, Alpine destroys
                // the <img> and recreating it triggers a visible reload
                // even with the same thumb URI. Genuine clears arrive
                // on the slot_updated channel, never via snapshot — this
                // won't mask user-initiated deletes.
                if (cur.thumb && !inc.thumb) {
                    incomingByIdx.delete(cur.index);
                    continue;
                }
                if (this._slotsDiffer(cur, inc)) {
                    target[i] = inc;
                }
                incomingByIdx.delete(cur.index);
            }
            // Append any new slots (first load, or topology grew)
            for (const inc of incomingByIdx.values()) {
                target.push(inc);
            }
            // Trim slots not present in the incoming topology (model swap)
            const incomingIdxSet = new Set(incoming.map(s => s.index));
            for (let i = target.length - 1; i >= 0; i--) {
                if (!incomingIdxSet.has(target[i].index)) target.splice(i, 1);
            }
        },

        _reconcileMediaPoolPlayers(target, incoming) {
            const incomingByIdx = new Map(incoming.map(p => [p.index, p]));
            for (let i = 0; i < target.length; i++) {
                const cur = target[i];
                const inc = incomingByIdx.get(cur.index);
                if (!inc) continue;
                if (this._playersDiffer(cur, inc)) {
                    target[i] = inc;
                }
                incomingByIdx.delete(cur.index);
            }
            for (const inc of incomingByIdx.values()) {
                target.push(inc);
            }
            const incomingIdxSet = new Set(incoming.map(p => p.index));
            for (let i = target.length - 1; i >= 0; i--) {
                if (!incomingIdxSet.has(target[i].index)) target.splice(i, 1);
            }
        },

        _getCsrfToken() {
            const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
            return m ? decodeURIComponent(m[1]) : '';
        },

        async uploadSlot(slotIndex, file) {
            console.log('[mediaPoolUpload] uploadSlot called', { slotIndex, fileName: file && file.name, currentIP: this.currentIP });
            if (!file) { console.warn('[mediaPoolUpload] no file, aborting'); return; }
            if (!this.currentIP || this.currentIP === '---.---.---.---') {
                console.warn('[mediaPoolUpload] no currentIP, aborting', this.currentIP);
                alert('Cannot upload: not connected to an ATEM');
                return;
            }
            if (this.isSlotUploading(slotIndex)) {
                console.warn('[mediaPoolUpload] slot already uploading, aborting');
                return;
            }

            this._markUploading(slotIndex);
            this._armUploadSafetyTimer(slotIndex);

            const fd = new FormData();
            fd.append('ip', this.currentIP);
            fd.append('slot', String(slotIndex));
            fd.append('image', file);

            const csrf = this._getCsrfToken();
            console.log('[mediaPoolUpload] POSTing', { csrfPresent: !!csrf, csrfLen: csrf.length });

            try {
                const resp = await fetch('/atem/media-pool-upload/', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-CSRFToken': csrf },
                    body: fd,
                });
                console.log('[mediaPoolUpload] response status', resp.status);
                const payload = await resp.json().catch((e) => {
                    console.warn('[mediaPoolUpload] failed to parse JSON', e);
                    return {};
                });
                console.log('[mediaPoolUpload] response payload', payload);
                if (!resp.ok || !payload.success) {
                    this._clearUploading(slotIndex);
                    const msg = payload.error || `Upload failed (${resp.status})`;
                    alert(`Slot ${slotIndex + 1}: ${msg}`);
                }
            } catch (e) {
                console.error('[mediaPoolUpload] fetch threw', e);
                this._clearUploading(slotIndex);
                alert(`Slot ${slotIndex + 1}: upload failed — ${e.message || e}`);
            }
        },

        // Tally a slot: on-air if any media player that's currently on-air
        // has this slot as its still source.
        isSlotOnAir(slotIndex) {
            for (const p of this.mediaPool.players) {
                if (p.type === 'still' && p.stillIndex === slotIndex) {
                    if (this.isMediaPlayerOnAir(p.index)) return true;
                }
            }
            return false;
        },

        // ATEM state - reactive with DVE support
        state: {
            // Per-M/E state lives under mes[me]. One entry per
            // M/E; every M/E is populated since the multi-M/E
            // flip. Must exist in the initial state so the updateState
            // merge (which only copies already-defined keys) picks up
            // mes. The entry's sub-shapes are the earlier
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
            // One entry per DSK (dsk → dsks[] restructure). Must
            // exist in the initial state so the updateState merge (which
            // only copies already-defined keys) picks up dsks.
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
            // up topology (the USK x-for renders zero sections
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
        // across name + IP + location, so "1.1 entain" finds
        // "BC 1.1 CL 01 - Entain". Rows whose NAME alone satisfies every
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
            stingerPreRoll: '1:18'
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

        setUSKDVEBorderBevelPosition(uskIndex, bevelPosition) {
            this.send('set_usk_dve_border_bevel_position', {key_index: uskIndex, bevelPosition: parseFloat(bevelPosition)});
        },

        setUSKDVEBorderBevelSoftness(uskIndex, bevelSoftness) {
            this.send('set_usk_dve_border_bevel_softness', {key_index: uskIndex, bevelSoftness: parseFloat(bevelSoftness)});
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
        // (the settings DSK sections render via x-for off
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
            // The switcher's self-assigned name (ATEM Setup), injected by
            // the consumer alongside the snapshot. Read from the raw
            // payload (NOT the merged state) so no merge-whitelist entry
            // is needed; feeds the header's name display via currentName.
            if (newState.atem_name !== undefined) {
                this.currentName = newState.atem_name || '';
            }

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
            // values until any user interaction triggered a re-render.
            // Diagnostic confirmed the data DID land in the
            // store; the bindings just weren't seeing the change.
            //
            // Reassigning ``state`` itself fires Alpine's outer-proxy
            // ``set`` trap, which forces every dependent effect to
            // re-evaluate against the new sub-tree.
            const merged = { ...this.state };
            Object.keys(newState).forEach(key => {
                if (merged[key] !== undefined) {
                    merged[key] = newState[key];
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
            const isEditing = (id) => document.activeElement?.id === id;
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
        },

        // Switch the rendered/controlled M/E. Everything that
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
            const colors = [
                'bg-green-600 border-green-600 text-white shadow-lg shadow-green-500/30',
                'bg-yellow-600 border-yellow-600 text-white shadow-lg shadow-yellow-500/30',
                'bg-blue-600 border-blue-600 text-white shadow-lg shadow-blue-500/30',
                'bg-gray-600 border-gray-600 text-white shadow-lg shadow-gray-500/30',
                'bg-red-600 border-red-600 text-white shadow-lg shadow-red-500/30'
            ];
            
            const isActive = this.state.mes[this.activeMe].transition.style === styleIndex;
            return isActive 
                ? `btn btn-sm ${colors[styleIndex]}`
                : 'btn btn-sm btn-outline hover:border-gray-400';
        },
        
        getTransitionSelectionButtonClass(keyType, keyIndex = null) {
            const selection = this.state.mes[this.activeMe].transition.selection;
            const isSelected = keyType === 'background' 
                ? selection.background 
                : selection[`key${keyIndex + 1}`];
            
            return isSelected
                ? 'btn control-btn text-xs bg-yellow-600 border-yellow-600 text-white shadow-lg shadow-yellow-500/30'
                : 'btn btn-outline control-btn text-xs hover:border-yellow-300';
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
            
            const baseClass = 'input input-sm border-blue-500 bg-blue-50 text-blue-700 font-semibold';
            
            // Check if in ending phase
            const inputElement = document.getElementById(countdown.inputId);
            if (inputElement) {
                const currentValue = inputElement.value;
                const [seconds, frames] = currentValue.split(':').map(Number);
                const totalFrames = (seconds || 0) * ATEMControl.fps() + (frames || 0);
                
                if (totalFrames <= 10) {
                    return `${baseClass} animate-pulse ring-2 ring-blue-400`;
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
        // WebSocket write buffer and causes multi-second atem_state
        // lag. We only subscribe while the operator has the audio
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

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    ATEMControl.init();
});