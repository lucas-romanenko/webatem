/* Realtime slider helper — drag registry + throttled send.
 *
 * Used by every <input type="range"> that drives a live ATEM control
 * (color gen, USK luma, wipe softness, DVE position, etc.). The
 * operator must see program output change while dragging, so we send
 * continuously during the drag (throttled) and use a path-keyed
 * registry to stop the polling-loop snapshot from overwriting the
 * field the operator is currently dragging.
 *
 * Audio faders use AudioFader instead — different convention.
 *
 * The post-release mute is wait-for-echo: we stay muted until the
 * server's polling snapshot reports a value within `tolerance` of the
 * value the operator released at, OR `muteMs` elapses (safety net).
 * A fixed time window doesn't work — ATEM processes throttled drag
 * commands sequentially and broadcasts state for each, so the catch-up
 * tail can outlast any reasonable fixed timeout and replay the drag
 * onto the slider after release.
 *
 * ATEM quantization: ATEM stores each control as an integer at some
 * wire scale (e.g. hue stored as deg×10, percent as ×100 or ×1000).
 * If the operator releases at a UI value not on the wire grid, the
 * echo comes back rounded — exact match never happens and the safety
 * timeout fires every drag, defeating the mechanism. Two defences:
 *   (1) Caller passes `step` (the slider's UI step). expectedValue is
 *       snapped to that grid on release so it matches what the ATEM
 *       will store.
 *   (2) Caller passes `tolerance` directly when wire quantization is
 *       coarser than UI step (rare but possible). Default tolerance
 *       is `step` (or `0.5` if no step given), which works when wire
 *       resolution is finer-or-equal to UI step.
 *
 * API:
 *   ATEMControl.dragRegistry.startDrag(path)
 *   ATEMControl.dragRegistry.endDrag(path, muteMs, expectedValue, tolerance)
 *   ATEMControl.dragRegistry.shouldSkip(path, observedValue)  -> per-field guard
 *   ATEMControl.dragRegistry.isGuarded(path)                  -> bare drag/mute check
 *   ATEMControl.dragRegistry.debug = true                     -> console logging
 *
 *   ATEMControl.RealtimeSlider.drag(path, value, sendFn, throttleMs = 40)
 *   ATEMControl.RealtimeSlider.release(path, value, sendFn, opts)
 *       opts: {muteMs = 3000, step, tolerance}
 *   ATEMControl.RealtimeSlider.cancel(path)
 *   ATEMControl.RealtimeSlider.cancelAll()
 *
 * Path convention: domain-prefixed dotted string, e.g.
 *   'atem:colorGen.0.hue', 'atem:usk.0.luma_clip', 'atem:wipe.softness'.
 * The 'atem:' prefix leaves room for 'x32:' sliders later without
 * collision risk.
 */
(function () {
    'use strict';

    window.ATEMControl = window.ATEMControl || {};

    const dragRegistry = {
        // path -> {dragging, muteUntil, expectedValue, tolerance, _logSeq}
        _entries: new Map(),

        // Toggle in console: window.ATEMControl.dragRegistry.debug = true
        debug: false,

        _log(msg) {
            if (this.debug) {
                console.log(`[RTS ${performance.now().toFixed(0)}ms] ${msg}`);
            }
        },

        startDrag(path) {
            const entry = this._entries.get(path) || {};
            entry.dragging = true;
            entry.muteUntil = 0;
            entry.expectedValue = undefined;
            entry.tolerance = 0;
            entry._logSeq = 0;
            this._entries.set(path, entry);
            this._log(`startDrag ${path}`);
        },

        endDrag(path, muteMs = 3000, expectedValue = undefined, tolerance = 0.5) {
            const entry = this._entries.get(path) || {};
            entry.dragging = false;
            entry.muteUntil = performance.now() + muteMs;
            entry.expectedValue = expectedValue;
            entry.tolerance = tolerance;
            entry._logSeq = 0;
            this._entries.set(path, entry);
            this._log(`endDrag   ${path} expected=${expectedValue} tol=${tolerance} mute=${muteMs}ms`);
            // Lazy cleanup so the map doesn't grow without bound on
            // many distinct paths.
            setTimeout(() => {
                const cur = this._entries.get(path);
                if (cur && !cur.dragging && cur.muteUntil <= performance.now()) {
                    this._entries.delete(path);
                }
            }, muteMs + 100);
        },

        // Per-field guard for updateState. Returns true if the caller
        // should skip applying observedValue to this slider. While
        // dragging, always skip. While muted, skip until the observed
        // value matches what the operator released at — that's the
        // signal that the ATEM has finished draining its command queue
        // and the slider can safely accept server state again.
        shouldSkip(path, observedValue) {
            const entry = this._entries.get(path);
            if (!entry) return false;
            if (entry.dragging) {
                this._log(`skip      ${path} v=${observedValue} (dragging)`);
                return true;
            }
            const now = performance.now();
            if (entry.muteUntil <= now) {
                if (entry.expectedValue !== undefined) {
                    this._log(`accept    ${path} v=${observedValue} expected=${entry.expectedValue} (SAFETY TIMEOUT — never matched)`);
                    entry.expectedValue = undefined;
                }
                return false;
            }
            if (entry.expectedValue !== undefined &&
                Math.abs(observedValue - entry.expectedValue) <= entry.tolerance) {
                this._log(`accept    ${path} v=${observedValue} expected=${entry.expectedValue} (echo matched, unmute)`);
                entry.muteUntil = 0;
                entry.expectedValue = undefined;
                return false;
            }
            entry._logSeq = (entry._logSeq || 0) + 1;
            const delta = entry.expectedValue !== undefined
                ? Math.abs(observedValue - entry.expectedValue).toFixed(3)
                : '?';
            this._log(`skip[#${entry._logSeq}] ${path} v=${observedValue} expected=${entry.expectedValue} delta=${delta} tol=${entry.tolerance}`);
            return true;
        },

        // Bare guard — true if dragging or in the mute window. Used
        // when the caller doesn't have an observed value to match
        // against (e.g. cleanup paths).
        isGuarded(path) {
            const entry = this._entries.get(path);
            if (!entry) return false;
            if (entry.dragging) return true;
            return entry.muteUntil > performance.now();
        },
    };

    const RealtimeSlider = {
        // path -> {timer, lastSentAt, pendingValue, sendFn}
        _state: new Map(),

        drag(path, value, sendFn, throttleMs = 40) {
            // Idempotent: if pointerdown fired we're already dragging;
            // for keyboard input there's no pointerdown, so register
            // here too. release() / cancel() unregister.
            if (!dragRegistry._entries.get(path)?.dragging) {
                dragRegistry.startDrag(path);
            }

            let s = this._state.get(path);
            if (!s) {
                s = { timer: null, lastSentAt: 0, pendingValue: value, sendFn };
                this._state.set(path, s);
            }
            s.pendingValue = value;
            s.sendFn = sendFn;

            const now = performance.now();
            const elapsed = now - s.lastSentAt;
            if (elapsed >= throttleMs) {
                // Leading edge: send now.
                s.sendFn(s.pendingValue);
                s.lastSentAt = now;
                if (s.timer) {
                    clearTimeout(s.timer);
                    s.timer = null;
                }
            } else if (!s.timer) {
                // Trailing edge: schedule one send for the end of the
                // throttle window. Subsequent drag() calls update
                // pendingValue without resetting the timer.
                const remaining = throttleMs - elapsed;
                s.timer = setTimeout(() => {
                    const cur = this._state.get(path);
                    if (!cur) return;
                    cur.timer = null;
                    cur.sendFn(cur.pendingValue);
                    cur.lastSentAt = performance.now();
                }, remaining);
            }
        },

        release(path, value, sendFn, opts = {}) {
            const muteMs = opts.muteMs ?? 3000;
            const step = opts.step;
            // Snap expectedValue to the UI step grid so it matches
            // what the ATEM will store after wire quantization. Without
            // this, releasing at a value off the grid (e.g. typed into
            // a number box) would never echo back exactly and the
            // safety timeout would fire every drag.
            const expected = (step && step > 0)
                ? Math.round(value / step) * step
                : value;
            // Default tolerance to the slider's step. Works when wire
            // resolution is finer-or-equal to UI step (the common
            // case). Caller overrides for sliders where wire is
            // coarser than UI step.
            const tolerance = opts.tolerance ?? (step ?? 0.5);
            const s = this._state.get(path);
            if (s && s.timer) {
                clearTimeout(s.timer);
                s.timer = null;
            }
            // Send the snapped value so the ATEM lands exactly where
            // the registry expects to see it echoed back.
            const sendValue = expected;
            (sendFn || s?.sendFn)?.(sendValue);
            this._state.delete(path);
            dragRegistry.endDrag(path, muteMs, expected, tolerance);
        },

        cancel(path) {
            const s = this._state.get(path);
            if (s && s.timer) {
                clearTimeout(s.timer);
                s.timer = null;
            }
            this._state.delete(path);
            dragRegistry.endDrag(path, 0);
        },

        // Cancel every in-flight throttled send AND every drag/mute guard
        // in the registry (paths in a post-release mute window have no
        // _state entry but still guard updateState). Called on WebSocket
        // close and on in-page ATEM switch — a dead connection can never
        // deliver the echo the mute windows are waiting for.
        cancelAll() {
            const paths = new Set([
                ...this._state.keys(),
                ...dragRegistry._entries.keys(),
            ]);
            for (const path of paths) {
                this.cancel(path);
            }
        },
    };

    window.ATEMControl.dragRegistry = dragRegistry;
    window.ATEMControl.RealtimeSlider = RealtimeSlider;
})();
