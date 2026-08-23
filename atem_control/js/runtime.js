/* runtime.js — the window.ATEMControl singleton: everything on the control
 * page that lives OUTSIDE the Alpine store.
 *
 * WebSocket lifecycle (init / connect / handleMessage feeding the store's
 * updateState), the cmd()/cmdAudio() send path every template action routes
 * through, the T-bar (DOM-direct drag + auto-transition animation), the
 * per-transition rate countdowns, the DVE transition style tables + grid
 * helpers, and the USKLightDrag polar pad.
 *
 * atem_realtime_slider.js (a separate script tag, loaded after the bundle)
 * attaches ATEMControl.dragRegistry + ATEMControl.RealtimeSlider onto this
 * object — that load order is a documented contract (e2e spec 08).
 */

window.ATEMControl = {
    ws: null,

    // ---- Declared-state-keys guard ------------------------------------
    // updateState merges ONLY top-level keys pre-declared in the store's
    // initial ``state`` literal (the merge-whitelist). A new
    // build_full_state key that isn't declared there used to VANISH
    // silently (bitten 3x — most recently `topology`, which broke the
    // USK x-for). An undeclared key now shouts once per key per page
    // load; the e2e net (e2e/helpers.js) treats this marker as a
    // failure, so it can't slip through a refactor. Deliberately a
    // console.error, never a throw — a new backend key must degrade,
    // not take down the operator page.
    _undeclaredStateKeys: new Set(),
    reportUndeclaredStateKey(key) {
        if (this._undeclaredStateKeys.has(key)) return;
        this._undeclaredStateKeys.add(key);
        console.error(
            `[ATEM] UNDECLARED STATE KEY DROPPED: '${key}' — build_full_state sent a ` +
            'top-level key that is not pre-declared in the initial state literal ' +
            '(atem_control.js, `state: {`), so it was NOT merged and the UI will ' +
            'never see it. Declare it there (with a matching shape) to fix.'
        );
    },

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
    // M/E index (Stage 4A) — the dispatch table threads it into the
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
            // instantly" optimization, but with the Issue #15
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

            // SH-13: the HyperDeck transport modal (and its poll timers)
            // belongs to the OLD ATEM's decks — left alone, its transport
            // buttons keep driving the previous ATEM's deck. close() stops
            // the timers + hides the modal; also forget the deck list so a
            // reopen starts from the NEW ATEM's RXMS bindings.
            if (window.AtemHyperdeck) window.AtemHyperdeck.close();
            alpineInstance.hyperdeck.deckIp = null;
            alpineInstance.hyperdeck.decks = [];
            // SH-27: ATEM-bound-but-unknown deck notice is per-ATEM.
            alpineInstance.hyperdeckUnknownDecks = [];

            // SH-27: per-slot "Uploading…" overlays (and their safety
            // timers) belong to the old ATEM's pool.
            for (const slot of alpineInstance.pendingUploads.slice()) {
                alpineInstance._clearUploading(slot);
            }

            // SH-13b: the meter subscription died with the old consumer —
            // clear the flag so the audio panel's x-effect re-subscribes
            // on the new connection, and run the painted bars down to
            // silence instead of freezing at the last received level.
            alpineInstance.audioMetersSubscribed = false;
            window.AudioMeters.reset();

            // L22: cancel every in-flight realtime-slider send + drag/mute
            // guard — they reference the old ATEM's values.
            window.ATEMControl.RealtimeSlider.cancelAll();

            // L25: a profile dialog opened against the old ATEM must not
            // apply its selections to the new one.
            if (window.AtemProfile && typeof window.AtemProfile.forceClose === 'function') {
                window.AtemProfile.forceClose();
            }

            // SH-28: gate the control surface behind the loading overlay
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

                // L22: cancel EVERY in-flight realtime-slider send and
                // drag/mute guard (was: just the selected color generator's
                // three paths) — a dead socket can't deliver the echo the
                // mute windows are waiting for.
                window.ATEMControl.RealtimeSlider.cancelAll();

                // SH-13b: the server-side meter subscription died with the
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
                        alpineInstance._finishUpload(slot.index);
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
                alpineInstance._finishUpload(slotIdx);
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
            case 'hyperdeck_unknown_decks': {
                // Decks bound on the ATEM that aren't in the equipment list.
                // Surface in the HyperDecks settings panel; never auto-modify.
                const store = Alpine.store('atem');
                if (store) store.hyperdeckUnknownDecks = Array.isArray(data.decks) ? data.decks : [];
                break;
            }
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
        if (rateInput) {
            // Restore what the operator had. An empty capture (the field
            // was cleared mid-edit when the countdown started) restores
            // empty; the next _syncMeRateInputs tick repopulates it from
            // switcher state.
            rateInput.value = countdown.originalValue || '';
            rateInput.classList.remove('rate-countdown-active', 'rate-countdown-ending');
        }
        // ALWAYS deactivate. This used to sit inside a truthy
        // originalValue guard, so a countdown that started while the
        // field was empty could never stop: active stayed true forever,
        // which froze the countdown leftover in the field AND disabled
        // the field's state sync (its guard checks active) until a page
        // reload. Found live 2026-08-22 — the "DVE rate dead both ways
        // while wipe works" report.
        countdown.originalValue = null;
        countdown.active = false;
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
