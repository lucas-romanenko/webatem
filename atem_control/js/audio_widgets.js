/* audio_widgets.js — the Fairlight audio panel's direct-DOM widgets.
 *
 * Everything here deliberately bypasses Alpine reactivity: meters arrive
 * at ~25 Hz per strip and faders/knobs paint every pointer move, and
 * routing that through the store would cause reactive thrash across the
 * page. The widgets attach to template-marked elements and talk back
 * through callbacks the templates pass to attach() — no imports from the
 * rest of the control page.
 *
 * Exposed on window (templates reference them from Alpine attributes):
 *   AudioMeters, AudioFader, AudioKnob, EQ_FILTER_ICONS
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
    // stream dies — WebSocket close or in-page ATEM switch (SH-13b).
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
