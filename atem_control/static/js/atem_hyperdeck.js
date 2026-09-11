/*
 * HyperDeck transport modal controller.
 *
 * Drives the control-page HyperDeck modal over the /atem/hyperdeck/ HTTP
 * endpoints. Full snapshot (slots + per-card clips + deck name) is fetched on
 * open / deck-switch / refresh; the lighter status endpoint (transport only) is
 * polled ~1 Hz for live status + timecode. All reactive state lives on
 * Alpine.store('atem').hyperdeck (+ .hyperdeckOpen).
 */
(function () {
    'use strict';

    var POLL_MS = 1000;          // transport status (light)
    var STATE_POLL_MS = 2000;    // slots + clip list (fuller, near-realtime)
    var TICK_MS = 100;           // render-tick: interpolated timecode update rate
    var pollTimer = null;
    var tickTimer = null;
    var statePollTimer = null;
    var inFlight = false;
    var stateInFlight = false;

    function store() {
        return (typeof Alpine !== 'undefined' && Alpine.store)
            ? Alpine.store('atem') : null;
    }

    function csrf() {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // --- timecode helpers (HH:MM:SS:FF) ---------------------------------------
    function fpsFromFormat(vf) {
        var m = (vf || '').match(/([0-9]+(?:\.[0-9]+)?)\s*$/);
        if (!m) return 25;
        var n = parseFloat(m[1]);
        if (n >= 1000) n = n / 100;        // "2997" -> 29.97, "5994" -> 59.94
        else if (n > 100) n = n / 10;
        return Math.round(n) || 25;
    }
    function tcToFrames(tc, fps) {
        var p = (tc || '').split(':');
        if (p.length < 4) return null;
        var n = p.map(function (x) { return parseInt(x, 10); });
        if (n.some(function (x) { return isNaN(x); })) return null;
        return ((n[0] * 60 + n[1]) * 60 + n[2]) * fps + n[3];
    }
    function framesToTc(f, fps) {
        if (f == null) return '';
        if (f < 0) f = 0;
        function pad(x) { return String(x).padStart(2, '0'); }
        var ff = f % fps; f = Math.floor(f / fps);
        var ss = f % 60; f = Math.floor(f / 60);
        var mm = f % 60; var hh = Math.floor(f / 60);
        return pad(hh) + ':' + pad(mm) + ':' + pad(ss) + ':' + pad(ff);
    }

    // --- fetch ----------------------------------------------------------------
    async function getJSON(url) {
        var r = await fetch(url);
        return r.json();
    }
    async function postTransport(ip, action, opts) {
        var body = Object.assign({ ip: ip, action: action }, opts || {});
        var r = await fetch('/atem/hyperdeck/transport/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
            body: JSON.stringify(body),
        });
        return r.json();
    }

    function applyTransport(hd, t) {
        hd.transport = t || {};
        hd.loop = (hd.transport.loop === 'true' || hd.transport.loop === true);
        // timestamp of this sample — the render ticker interpolates the
        // playhead between 1 Hz polls so the timecode counts up smoothly
        hd.sampleMs = Date.now();
    }

    function isPlayingStatus(s) {
        s = (s || '').toLowerCase();
        return s === 'play' || s === 'playing' || s === 'forward';
    }

    // Playhead position in frames: the last polled timecode, advanced locally
    // by wall-clock time while playing (resynced to truth on every poll).
    function playheadFrames(hd, fps, durFrames) {
        var t = hd.transport || {};
        var ef = tcToFrames(t['timecode'] || t['display timecode'] || '', fps);
        if (ef == null) return null;
        if (isPlayingStatus(t.status) && hd.sampleMs) {
            var speed = parseInt(t.speed, 10);
            if (isNaN(speed) || speed === 0) speed = 100;
            ef += Math.floor((Date.now() - hd.sampleMs) / 1000 * fps * (speed / 100));
        }
        if (durFrames) {
            ef = hd.loop ? (ef % durFrames) : Math.min(ef, durFrames);
        }
        return Math.max(0, ef);
    }

    function applyState(hd, data) {
        if (data && data.error) { hd.error = data.error; if (data.name) hd.name = data.name; return; }
        hd.error = null;
        hd.name = (data && data.name) || null;
        hd.model = (data && data.model) || '';
        hd.videoFormat = (data && data.video_format) || '';
        hd.slots = (data && data.slots) || [];
        hd.clips = (data && data.clips) || [];
        applyTransport(hd, data && data.transport);
    }

    var AtemHyperdeck = {
        open: function () {
            var st = store(); if (!st) return;
            var hd = st.hyperdeck;
            // Every configured deck bound on the ATEM (state.hyperdecks = RXMS).
            // Each sidebar entry carries its own name + reachability + last
            // known transport status, loaded in parallel below.
            var decks = ((st.state && st.state.hyperdecks) || [])
                .filter(function (d) { return d.configured; })
                .map(function (d) {
                    return { slot: d.slot, network_address: d.network_address,
                             input: d.input, name: null,
                             online: null,            // null=checking, true/false
                             playState: null };       // last transport status string
                });
            hd.decks = decks;
            hd.deckIp = decks.length ? decks[0].network_address : null;
            hd.clips = []; hd.slots = []; hd.transport = {}; hd.name = null; hd.model = '';
            hd.error = decks.length ? null : 'No HyperDecks are configured on this ATEM.';
            st.hyperdeckOpen = true;
            if (hd.deckIp) {
                this.refresh(true);
                this.startPoll();
                this._loadDeckOverview();
            }
        },

        // Patch a sidebar deck entry by IP and reassign the array so Alpine
        // re-renders (must mutate the live store array, not a captured copy —
        // the captured copy holds raw objects, not Alpine's reactive proxies).
        _updateDeckEntry: function (ip, patch) {
            var st = store(); if (!st) return;
            var decks = st.hyperdeck.decks;
            var changed = false;
            for (var i = 0; i < decks.length; i++) {
                if (decks[i].network_address === ip) {
                    Object.assign(decks[i], patch);
                    changed = true;
                }
            }
            if (changed) st.hyperdeck.decks = decks.slice();
        },

        // Per-deck sidebar info: Equipment-list name + reachability + transport
        // status, fetched for every deck in parallel.
        _loadDeckOverview: function () {
            var self = this;
            store().hyperdeck.decks.forEach(function (d) {
                var ip = d.network_address;
                fetch('/atem/api/lookup-name/?ip=' + encodeURIComponent(ip))
                    .then(function (r) { return r.json(); })
                    .then(function (j) { if (j && j.found && j.name) self._updateDeckEntry(ip, { name: j.name }); })
                    .catch(function () {});
                getJSON('/atem/hyperdeck/status/?ip=' + encodeURIComponent(ip))
                    .then(function (j) {
                        if (j && !j.error) self._updateDeckEntry(ip, { online: true, playState: (j.transport || {}).status || null });
                        else self._updateDeckEntry(ip, { online: false });
                    })
                    .catch(function () { self._updateDeckEntry(ip, { online: false }); });
            });
        },

        // Reconcile the open modal's sidebar against the LIVE switcher bindings
        // (state.hyperdecks). Fired reactively while the modal is open, so a
        // deck cleared or added in ASC disappears/appears without reopening —
        // the modal snapshots hd.decks on open, this keeps it honest. Cheap:
        // bails immediately when the configured-deck set is unchanged.
        syncDecks: function () {
            var st = store(); if (!st || !st.hyperdeckOpen) return;
            var hd = st.hyperdeck;
            var configured = ((st.state && st.state.hyperdecks) || [])
                .filter(function (d) { return d.configured; });
            var wantIps = configured.map(function (d) { return d.network_address; });
            var haveIps = hd.decks.map(function (d) { return d.network_address; });
            var same = wantIps.length === haveIps.length &&
                wantIps.every(function (ip) { return haveIps.indexOf(ip) !== -1; });
            if (same) return;

            // drop decks no longer bound; keep the rest (with their live status)
            var next = hd.decks.filter(function (d) { return wantIps.indexOf(d.network_address) !== -1; });
            var added = false;
            configured.forEach(function (d) {
                if (!next.some(function (x) { return x.network_address === d.network_address; })) {
                    next.push({ slot: d.slot, network_address: d.network_address,
                                input: d.input, name: null, online: null, playState: null });
                    added = true;
                }
            });
            hd.decks = next;

            if (hd.deckIp && wantIps.indexOf(hd.deckIp) === -1) {
                // the selected deck vanished — move to the first remaining, or empty out
                if (hd.decks.length) {
                    hd.deckIp = hd.decks[0].network_address;
                    hd.clips = []; hd.slots = []; hd.transport = {}; hd.name = null; hd.error = null;
                    this.refresh(true); this._loadDeckOverview();
                } else {
                    hd.deckIp = null; hd.clips = []; hd.slots = []; hd.transport = {};
                    hd.name = null; hd.model = '';
                    hd.error = 'No HyperDecks are configured on this ATEM.';
                    this.stopPoll();
                }
            } else if (added) {
                this._loadDeckOverview();
            }
        },

        close: function () {
            var st = store(); if (!st) return;
            st.hyperdeckOpen = false;
            this.stopPoll();
        },

        selectDeck: function (ip) {
            var st = store(); if (!st) return;
            st.hyperdeck.deckIp = ip;
            st.hyperdeck.clips = []; st.hyperdeck.slots = [];
            st.hyperdeck.transport = {}; st.hyperdeck.name = null;
            this.refresh(true);
        },

        refresh: async function (showLoading) {
            var st = store(); if (!st) return;
            var hd = st.hyperdeck;
            if (!hd.deckIp) return;
            if (showLoading) hd.loading = true;
            var ip = hd.deckIp;
            try {
                var data = await getJSON('/atem/hyperdeck/state/?ip=' + encodeURIComponent(ip));
                applyState(hd, data);
                // the selected deck demonstrably reached us — reflect that in its
                // sidebar entry so it never sits on "checking…"
                if (data && !data.error) {
                    this._updateDeckEntry(ip, { online: true, playState: (data.transport || {}).status || null });
                } else {
                    this._updateDeckEntry(ip, { online: false });
                }
            } catch (e) {
                hd.error = 'Could not reach the deck.';
                this._updateDeckEntry(ip, { online: false });
            } finally {
                hd.loading = false;
            }
        },

        startPoll: function () {
            this.stopPoll();
            pollTimer = setInterval(function () {
                var st = store();
                if (!st || !st.hyperdeckOpen || !st.hyperdeck.deckIp || inFlight) return;
                inFlight = true;
                var pollIp = st.hyperdeck.deckIp;
                getJSON('/atem/hyperdeck/status/?ip=' + encodeURIComponent(pollIp))
                    .then(function (d) {
                        if (d && !d.error) {
                            applyTransport(st.hyperdeck, d.transport);
                            AtemHyperdeck._updateDeckEntry(pollIp, { online: true, playState: (d.transport || {}).status || null });
                        }
                    })
                    .catch(function () { /* keep last known */ })
                    .finally(function () { inFlight = false; });
            }, POLL_MS);
            // Render tick: while playing, bump a reactive counter so the
            // timecode/progress bindings re-evaluate and the interpolated
            // playhead counts up smoothly between the 1 Hz polls.
            tickTimer = setInterval(function () {
                var st = store();
                if (!st || !st.hyperdeckOpen) return;
                if (isPlayingStatus((st.hyperdeck.transport || {}).status)) {
                    st.hyperdeck.tick = ((st.hyperdeck.tick || 0) + 1) % 1000000;
                }
            }, TICK_MS);
            // Slower full-state poll: refreshes SD-card status + the clip list
            // so adding/removing media or swapping cards shows up near-realtime.
            // Silent (no spinner) and leaves transport to the 1 Hz status poll.
            statePollTimer = setInterval(function () { AtemHyperdeck._pollState(); }, STATE_POLL_MS);
        },

        // Silent slots+clips refresh (the deck's media can change while open).
        _pollState: function () {
            var st = store();
            if (!st || !st.hyperdeckOpen || !st.hyperdeck.deckIp || stateInFlight) return;
            stateInFlight = true;
            var ip = st.hyperdeck.deckIp;
            getJSON('/atem/hyperdeck/state/?ip=' + encodeURIComponent(ip))
                .then(function (d) {
                    // ignore if the deck was switched or modal closed mid-flight
                    if (!st.hyperdeckOpen || st.hyperdeck.deckIp !== ip) return;
                    if (d && !d.error) {
                        var hd = st.hyperdeck;
                        if (d.name) hd.name = d.name;
                        if (d.model) hd.model = d.model;
                        if (d.video_format) hd.videoFormat = d.video_format;
                        hd.slots = d.slots || [];
                        hd.clips = d.clips || [];
                        // transport stays owned by the 1 Hz status poll + interpolation
                    }
                })
                .catch(function () { /* keep last known */ })
                .finally(function () { stateInFlight = false; });
        },

        stopPoll: function () {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
            if (statePollTimer) { clearInterval(statePollTimer); statePollTimer = null; }
        },

        _transport: async function (action, opts) {
            var st = store(); if (!st) return;
            var hd = st.hyperdeck;
            if (!hd.deckIp) return;
            try {
                var d = await postTransport(hd.deckIp, action, opts);
                if (d && d.error) hd.error = d.error;
                else { hd.error = null; applyTransport(hd, d && d.transport); }
            } catch (e) {
                hd.error = 'Command failed.';
            }
        },

        play: function () {
            var hd = store().hyperdeck;
            this._transport('play', { loop: hd.loop, single_clip: hd.loop });
        },
        playClip: function (c) {
            var hd = store().hyperdeck;
            this._transport('play', { clip_id: c.clip_id, slot: c.slot, loop: hd.loop, single_clip: hd.loop });
        },
        cueClip: function (c) { this._transport('goto', { clip_id: c.clip_id, slot: c.slot }); },
        pause: function () { this._transport('pause'); },
        stop: function () { this._transport('stop'); },

        toggleLoop: function () {
            var hd = store().hyperdeck;
            hd.loop = !hd.loop;
            if (this.isPlaying()) this.play();
        },

        isPlaying: function () {
            var s = ((store().hyperdeck.transport.status) || '').toLowerCase();
            return s === 'play' || s === 'playing' || s === 'forward';
        },

        // currently-playing clip (matched by clip id + active slot), or null
        playingClip: function () {
            var hd = store().hyperdeck, t = hd.transport || {};
            return hd.clips.find(function (c) {
                return String(c.clip_id) === String(t['clip id']) &&
                    (t['slot id'] == null || String(c.slot) === String(t['slot id']));
            }) || null;
        },

        isClipPlaying: function (c) {
            var t = store().hyperdeck.transport || {};
            return String(c.clip_id) === String(t['clip id']) &&
                String(c.slot) === String(t['slot id']);
        },

        // playback progress 0–100 for the Now-Playing bar
        progress: function () {
            var hd = store().hyperdeck;
            void hd.tick;   // re-evaluate on each render tick
            var fps = fpsFromFormat(hd.videoFormat || (hd.transport || {})['video format']);
            var pc = this.playingClip();
            var df = pc ? tcToFrames(pc.duration, fps) : null;
            if (!df) return 0;
            var ef = playheadFrames(hd, fps, df);
            if (ef == null) return 0;
            return Math.max(0, Math.min(100, (ef / df) * 100));
        },

        // {length, elapsed, remaining} for the playing clip (display strings)
        times: function () {
            var hd = store().hyperdeck;
            void hd.tick;   // re-evaluate on each render tick
            var fps = fpsFromFormat(hd.videoFormat || (hd.transport || {})['video format']);
            var pc = this.playingClip();
            var dur = pc ? pc.duration : '';
            var df = tcToFrames(dur, fps);
            var ef = playheadFrames(hd, fps, df);
            var rem = (ef != null && df != null) ? framesToTc(Math.max(0, df - ef), fps) : '';
            return {
                length: dur || '—',
                elapsed: (ef != null) ? framesToTc(ef, fps) : '—',
                remaining: rem || '—',
            };
        },
    };

    window.AtemHyperdeck = AtemHyperdeck;
})();
