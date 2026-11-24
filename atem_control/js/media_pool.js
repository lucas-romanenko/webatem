/* media_pool.js — the media-pool modal's slice of the Alpine store.
 *
 * Spread into the store literal in store.js (templates keep reading
 * $store.atem.mediaPool / getSlot / uploadSlot / ...). Slot + player
 * arrays are pushed from the server over the Channels group
 * (mediapool.snapshot / slot_updated / player_updated — see the Python
 * side, atem_control/media_pool/); this slice owns the
 * browser end: keyed reconcile (no wholesale array replace, so open
 * tooltips/hover states survive), the drag-drop uploadSlot POST, the
 * optimistic clear/set-player ops, upload progress rings (driven by the
 * server's media_pool_upload_progress; wording in upload_status.js), and
 * slot/player on-air tally.
 *
 * Methods and plain data ONLY — no getters (spread would evaluate
 * them; see the note in store.js).
 */

import {
    LOST_AFTER_S, captureStallText, ringPercent, stalledSeconds, uploadFailureText, uploadLabel, uploadStallText,
} from './upload_status.js';

export const mediaPoolStore = {
    // Media pool — pushed from server via Channels group, real-time multi-user.
    // Starts empty; the first snapshot from the server populates arrays at
    // the ATEM's actual topology (e.g. 20 slots / 2 players on a TVS HD).
    mediaPool: {
        slots: [],
        players: [],
        // True once previews have waited a while for another app's turn
        // at the switcher's media pool (ATEM Software Control loading its
        // thumbnails, say). Drives the waiting notice; never "locked".
        waiting: false,
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
    // slot -> {stage, percent, movedAt}: what the server last said the
    // upload was doing (media_pool_upload_progress) and when it last
    // moved. The ring and its words come from this alone (upload_status.js).
    uploadStatus: {},
    // [{slot, text}]: failed uploads, said in the panel until dismissed or
    // the slot is uploaded to again.
    uploadErrors: [],
    // Wall clock for the "no progress for N s" words, ticked once a second
    // while an upload is pending (bindings that read it re-evaluate).
    uploadClock: 0,
    _uploadClockTimer: null,       // setInterval handle (non-reactive use)

    isSlotUploading(slotIndex) {
        return this.pendingUploads.includes(slotIndex);
    },

    _markUploading(slotIndex) {
        if (!this.pendingUploads.includes(slotIndex)) {
            this.pendingUploads.push(slotIndex);
        }
        this.uploadErrors = this.uploadErrors.filter(e => e.slot !== slotIndex);
        this.uploadStatus[slotIndex] = { stage: null, percent: null, movedAt: Date.now() };
        this._tickUploadClock();
        // Wait one frame for the overlay to render, then show the ring as
        // "don't know yet" (spinning) until the switcher grants a budget.
        requestAnimationFrame(() => this._paintUploadRing(slotIndex));
    },

    _clearUploading(slotIndex) {
        delete this.uploadStatus[slotIndex];
        const i = this.pendingUploads.indexOf(slotIndex);
        if (i >= 0) this.pendingUploads.splice(i, 1);
        if (!this.pendingUploads.length) this._stopUploadClock();
    },

    // The server says how an upload is moving: {slot, stage, percent}.
    // A stage change or a higher percent is movement and restarts the
    // stall clock; a repeat is not. Uploads this page didn't start are
    // left alone (their tiles update when the slot does).
    noteUploadProgress(payload) {
        const slotIndex = payload && payload.slot;
        if (typeof slotIndex !== 'number' || !this.isSlotUploading(slotIndex)) return;
        const cur = this.uploadStatus[slotIndex] || {};
        const percent = typeof payload.percent === 'number' ? payload.percent : null;
        const moved = payload.stage !== cur.stage
            || (percent !== null && (cur.percent === null || percent > cur.percent));
        this.uploadStatus[slotIndex] = {
            stage: payload.stage || cur.stage || null,
            percent: percent !== null ? percent : (payload.stage === cur.stage ? cur.percent : null),
            movedAt: moved ? Date.now() : cur.movedAt,
        };
        this._paintUploadRing(slotIndex);
    },

    // --- Upload progress ring (drag-drop onto a slot) ---
    // Fills to the percent of the still the switcher has really granted
    // (bmdwire's upload-progress, relayed by the server); spins while
    // there is no such number (connecting, preparing, verifying). Only the
    // server's word that the upload ended fills it to 100 (_finishUpload).
    // It used to ease towards 90 % on a timer whatever the switcher did,
    // so a stalled upload looked like one about to finish.
    // JS owns the DOM directly (no Alpine binding on stroke-dashoffset — it
    // would fight these writes, the same failure mode as the sliders).
    _RING_CIRCUMFERENCE: 100.53,   // 2*pi*16, matches the template's r=16
    _RING_SPIN_PCT: 25,            // the arc shown while it spins

    _paintRing(slotIndex, pct) {
        const el = document.querySelector('[data-upload-ring="' + slotIndex + '"]');
        if (el) {
            const off = this._RING_CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, pct)) / 100);
            el.setAttribute('stroke-dashoffset', String(off));
        }
    },

    _paintUploadRing(slotIndex) {
        const pct = ringPercent(this.uploadStatus[slotIndex]);
        this._paintRing(slotIndex, pct === null ? this._RING_SPIN_PCT : pct);
    },

    // Template helpers: spin or fill, the line under the ring, and the
    // panel's sentences for uploads that have gone quiet.
    uploadRingSpins(slotIndex) {
        return ringPercent(this.uploadStatus[slotIndex]) === null;
    },

    uploadLabelFor(slotIndex) {
        return uploadLabel(this.uploadStatus[slotIndex], this.uploadClock || Date.now());
    },

    uploadStallLines() {
        const now = this.uploadClock || Date.now();
        const out = [];
        for (const slot of this.pendingUploads) {
            const text = uploadStallText(slot, this.uploadStatus[slot], now);
            if (text) out.push({ slot, text });
        }
        return out;
    },

    _tickUploadClock() {
        this.uploadClock = Date.now();
        if (!this._uploadClockTimer) {
            this._uploadClockTimer = setInterval(() => this._onUploadClock(), 1000);
        }
    },

    _stopUploadClock() {
        if (this._uploadClockTimer) {
            clearInterval(this._uploadClockTimer);
            this._uploadClockTimer = null;
        }
    },

    // Once a second while uploads are pending: move the clock, and give up
    // on one the server has said nothing about for LOST_AFTER_S (the
    // server process went away, say) — out loud, never silently.
    _onUploadClock() {
        this.uploadClock = Date.now();
        for (const slot of this.pendingUploads.slice()) {
            if (stalledSeconds(this.uploadStatus[slot], this.uploadClock) >= LOST_AFTER_S) {
                this.failUpload(slot,
                    `no word about it from the server for ${LOST_AFTER_S} s, so this page has stopped waiting`,
                    null);
            }
        }
        if (!this.pendingUploads.length) this._stopUploadClock();
    },

    // Success completion: fill the ring to 100%, hold briefly so the fill
    // reads, then clear the overlay (it fades out, revealing the thumbnail
    // the caller already set). Idempotent + safe if the slot isn't pending.
    _finishUpload(slotIndex) {
        if (!this.isSlotUploading(slotIndex)) { this._clearUploading(slotIndex); return; }
        if (this.uploadStatus[slotIndex]) this.uploadStatus[slotIndex].stage = 'done';
        this._paintRing(slotIndex, 100);
        setTimeout(() => this._clearUploading(slotIndex), 240);
    },

    // Failure: clear the overlay and say why in the panel, with what it
    // changed on the switcher (`changed`: false = nothing, null = unknown).
    failUpload(slotIndex, reason, changed) {
        this._clearUploading(slotIndex);
        this.uploadErrors = this.uploadErrors.filter(e => e.slot !== slotIndex);
        this.uploadErrors.push({ slot: slotIndex, text: uploadFailureText(slotIndex, reason, changed) });
    },

    // Profile save's capture: its "no slot finished for N s" line.
    profileCaptureStallText() {
        return captureStallText(this.profileCaptureProgress,
                                this.profileCaptureClock || Date.now());
    },

    dismissUploadError(slotIndex) {
        this.uploadErrors = this.uploadErrors.filter(e => e.slot !== slotIndex);
    },

    uploadErrorFor(slotIndex) {
        const e = this.uploadErrors.find(x => x.slot === slotIndex);
        return e ? e.text : '';
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
            || a.thumb !== b.thumb
            || !!a.previewFailed !== !!b.previewFailed;
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
                // Refused before anything went to the switcher.
                this.failUpload(slotIndex, payload.error || `the server refused it (HTTP ${resp.status})`, false);
            }
        } catch (e) {
            // The request may or may not have reached the server.
            console.error('[mediaPoolUpload] fetch threw', e);
            this.failUpload(slotIndex, `this page could not reach the server (${e.message || e})`, null);
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
};
