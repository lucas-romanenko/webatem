/**
 * ATEM Connect — WebSocket-based connection flow with auto-connect support.
 *
 * Expects the following data attributes on the page:
 *   form[data-control-url]  – URL to redirect to after successful connection
 */

(function () {
    'use strict';

    var ws = null;
    var connectionAttempting = false;
    var waitingForState = false;

    // Read the ATEM control page URL from the form's data attribute
    var connectForm = document.querySelector('form[data-control-url]');
    var controlUrl = connectForm ? connectForm.dataset.controlUrl : '/atem/control/';

    // ATEM inventory ([{name, ip, location}]) rendered by the view into a
    // json_script block. Backs the name-or-IP suggestion dropdown.
    var atemEquipment = [];
    try {
        var eqEl = document.getElementById('atem-equipment-data');
        if (eqEl) atemEquipment = JSON.parse(eqEl.textContent) || [];
    } catch (e) { /* suggestions just stay empty */ }

    var IP_SHAPE = /^\d{1,3}(\.\d{1,3}){3}$/;
    var suggestIndex = 0;

    function queryTokens(query) {
        return query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    }

    function nameMatchesAll(e, tokens) {
        var name = e.name.toLowerCase();
        return tokens.every(function (t) { return name.indexOf(t) !== -1; });
    }

    // One token against one row: substring on name/location, but IP hits
    // must align to an octet boundary — "81.1" narrows 81.1xx, while a
    // room-number token like "1.1" can't ghost-match the middle of 81.140.
    function tokenMatches(e, t) {
        return e.name.toLowerCase().indexOf(t) !== -1 ||
            (e.location || '').toLowerCase().indexOf(t) !== -1 ||
            ('.' + e.ip).indexOf('.' + t) !== -1;
    }

    // Token search: every space-separated token must hit somewhere across
    // name + IP + location, so "2 north" finds "Studio 2 - North Hall".
    // Rows whose NAME alone satisfies every token rank first.
    function equipmentMatches(query) {
        var tokens = queryTokens(query);
        if (!tokens.length) return [];
        var out = atemEquipment.filter(function (e) {
            return tokens.every(function (t) { return tokenMatches(e, t); });
        });
        out.sort(function (a, b) { return nameMatchesAll(b, tokens) - nameMatchesAll(a, tokens); });
        return out;
    }

    // Resolve what the operator typed to a connect target.
    // Literal IPs always win; otherwise an exact name match, then a unique
    // filter match. Returns {ip} on success, or {error} for the status box.
    function resolveTarget(raw) {
        if (IP_SHAPE.test(raw)) return { ip: raw };
        var matches = equipmentMatches(raw);
        var exact = matches.filter(function (e) {
            return e.name.toLowerCase() === raw.toLowerCase();
        })[0];
        if (exact) return { ip: exact.ip };
        if (matches.length === 1) return { ip: matches[0].ip };
        // A unique NAME hit wins over IP/location coincidences
        var nameHits = matches.filter(function (e) { return nameMatchesAll(e, queryTokens(raw)); });
        if (nameHits.length === 1) return { ip: nameHits[0].ip };
        if (matches.length === 0) {
            return { error: 'No ATEM matches "' + raw + '" - enter an IP address or equipment name' };
        }
        return { error: 'Multiple ATEMs match "' + raw + '" - pick one from the list' };
    }

    function hideSuggestions() {
        var list = document.getElementById('ipSuggestions');
        list.classList.add('hidden');
        list.innerHTML = '';
    }

    function renderSuggestions() {
        var list = document.getElementById('ipSuggestions');
        var input = document.getElementById('ipAddress');
        var matches = equipmentMatches(input.value).slice(0, 8);

        if (!matches.length) { hideSuggestions(); return; }
        if (suggestIndex >= matches.length) suggestIndex = 0;

        list.innerHTML = '';
        matches.forEach(function (m, i) {
            var li = document.createElement('li');
            var a = document.createElement('a');
            a.href = 'javascript:void(0)';
            if (i === suggestIndex) a.classList.add('active');

            // textContent throughout — equipment names are operator-entered
            var name = document.createElement('span');
            name.className = 'font-medium';
            name.textContent = m.name;
            var detail = document.createElement('span');
            detail.className = 'text-xs text-base-content/60 font-mono';
            detail.textContent = m.ip + (m.location ? ' — ' + m.location : '');

            var col = document.createElement('div');
            col.className = 'flex flex-col items-start';
            col.appendChild(name);
            col.appendChild(detail);
            a.appendChild(col);

            // mousedown (not click) so selection wins the race with the
            // input's blur handler hiding the list
            a.addEventListener('mousedown', function (ev) {
                ev.preventDefault();
                selectSuggestion(m);
            });
            li.appendChild(a);
            list.appendChild(li);
        });
        list.classList.remove('hidden');
    }

    function selectSuggestion(match) {
        document.getElementById('ipAddress').value = match.ip;
        hideSuggestions();
        connectToAtem(new Event('submit'));
    }

    window.setIP = function (ip) {
        document.getElementById('ipAddress').value = ip;
        hideSuggestions();
    };

    function showStatus(message, type) {
        type = type || 'info';
        var statusDiv = document.getElementById('statusMessage');
        var statusText = document.getElementById('statusMessageText');

        statusText.textContent = message;

        // Remove all alert classes
        statusDiv.className = 'alert';

        switch (type) {
            case 'error': statusDiv.classList.add('alert-error'); break;
            case 'success': statusDiv.classList.add('alert-success'); break;
            case 'warning': statusDiv.classList.add('alert-warning'); break;
            case 'info':
            default: statusDiv.classList.add('alert-info'); break;
        }

        statusDiv.classList.remove('hidden');
    }

    function hideStatus() {
        document.getElementById('statusMessage').classList.add('hidden');
    }

    function setLoading(loading) {
        var btn = document.getElementById('connectBtn');

        if (loading) {
            btn.disabled = true;
            btn.innerHTML = '<span class="atem-connect-btn-inner"><span class="loading loading-spinner loading-sm"></span>Connecting...</span>';
        } else {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-plug mr-2"></i>Connect to ATEM';
        }
    }

    function setLoadingText(text) {
        var btn = document.getElementById('connectBtn');
        btn.innerHTML = '<span class="atem-connect-btn-inner"><span class="loading loading-spinner loading-sm"></span>' + text + '</span>';
    }

    window.connectToAtem = function (event) {
        event.preventDefault();

        if (connectionAttempting) return;

        var rawTarget = document.getElementById('ipAddress').value.trim();
        if (!rawTarget) {
            showStatus('Please enter an IP address or equipment name', 'error');
            return;
        }

        // Name-or-IP: resolve equipment names to their IP before connecting
        var resolved = resolveTarget(rawTarget);
        if (resolved.error) {
            showStatus(resolved.error, 'error');
            return;
        }
        var ipAddress = resolved.ip;
        hideSuggestions();

        connectionAttempting = true;
        setLoading(true);
        hideStatus();

        // Set timeout for entire connection process
        var connectionTimeout = setTimeout(function () {
            if (connectionAttempting) {
                showStatus('Connection timeout - please check IP address and try again', 'error');
                setLoading(false);
                connectionAttempting = false;
                if (ws) {
                    ws.close();
                    ws = null;
                }
            }
        }, 15000);

        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = protocol + '//' + window.location.host + '/ws/atem/';

        try {
            ws = new WebSocket(wsUrl);

            ws.onopen = function () {
                ws.send(JSON.stringify({
                    command: 'connect',
                    ip_address: ipAddress,
                    test_connection: true
                }));
            };

            ws.onmessage = function (event) {
                try {
                    var data = JSON.parse(event.data);

                    if (data.type === 'connection_status') {
                        if (data.connected) {
                            // ATEM connected, now request initial state
                            waitingForState = true;
                            setLoadingText('Loading ATEM state...');

                            ws.send(JSON.stringify({
                                command: 'refresh'
                            }));

                        } else {
                            clearTimeout(connectionTimeout);
                            connectionAttempting = false;
                            setLoading(false);

                            var errorMsg = data.message || 'Failed to connect to ATEM';
                            if (errorMsg.includes('timeout')) {
                                errorMsg = 'ATEM connection timeout - check IP address';
                            } else if (errorMsg.includes('refused') || errorMsg.includes('failed')) {
                                errorMsg = 'Cannot reach ATEM - check IP address and network';
                            }

                            showStatus(errorMsg, 'error');

                            if (ws) {
                                ws.close();
                                ws = null;
                            }
                        }
                    } else if (data.type === 'atem_state' && waitingForState) {
                        // Got ATEM state, now we can redirect
                        clearTimeout(connectionTimeout);
                        localStorage.setItem('atem_ip', ipAddress);

                        // Store the initial state so the control page can use it immediately
                        localStorage.setItem('atem_initial_state', JSON.stringify(data.state));

                        // Only preserve name parameter if it came from Equipment List
                        var urlParams = new URLSearchParams(window.location.search);
                        var equipmentName = urlParams.get('name');
                        var isFromEquipmentList = urlParams.get('connect') && equipmentName;

                        var redirectParams = '';
                        if (isFromEquipmentList) {
                            redirectParams = '?ip=' + ipAddress + '&name=' + encodeURIComponent(equipmentName);
                            window.location.href = controlUrl + redirectParams;
                        } else {
                            // Manual connection — lookup name from database
                            fetch('/atem/api/lookup-name/?ip=' + encodeURIComponent(ipAddress))
                                .then(function (response) { return response.json(); })
                                .then(function (lookupData) {
                                    if (lookupData.found && lookupData.name) {
                                        redirectParams = '?ip=' + ipAddress + '&name=' + encodeURIComponent(lookupData.name);
                                    } else {
                                        redirectParams = '?ip=' + ipAddress;
                                    }
                                    window.location.href = controlUrl + redirectParams;
                                })
                                .catch(function () {
                                    redirectParams = '?ip=' + ipAddress;
                                    window.location.href = controlUrl + redirectParams;
                                });
                        }
                    }
                } catch (e) {
                    console.error('Error parsing message:', e);
                }
            };

            ws.onclose = function () {
                if (connectionAttempting) {
                    clearTimeout(connectionTimeout);
                    showStatus('Connection failed - please check network and try again', 'error');
                    setLoading(false);
                    connectionAttempting = false;
                }
            };

            ws.onerror = function () {
                clearTimeout(connectionTimeout);
                showStatus('Network error - please check connection', 'error');
                setLoading(false);
                connectionAttempting = false;
            };

        } catch (error) {
            clearTimeout(connectionTimeout);
            showStatus('Failed to create connection: ' + error.message, 'error');
            setLoading(false);
            connectionAttempting = false;
        }
    };

    // Auto-focus IP input and handle auto-connect
    window.addEventListener('load', function () {
        var urlParams = new URLSearchParams(window.location.search);
        var autoConnectIP = urlParams.get('connect');
        var equipmentName = urlParams.get('name');

        // Surface a disconnect reason stashed by the control page when it
        // redirected back here (cable yank, 5-minute inactivity timeout,
        // any other unexpected disconnect). sessionStorage clears
        // naturally on tab close; we explicitly clear after reading so
        // a refresh doesn't re-show stale messages.
        try {
            var disconnectReason = sessionStorage.getItem('atem_disconnect_reason');
            if (disconnectReason) {
                showStatus(disconnectReason, 'warning');
                sessionStorage.removeItem('atem_disconnect_reason');
            }
        } catch (e) { /* sessionStorage may be unavailable in private mode */ }

        if (autoConnectIP) {
            document.getElementById('ipAddress').value = autoConnectIP;

            if (equipmentName) {
                document.querySelector('h1').textContent = 'Connect to ' + equipmentName;
                document.querySelector('p').textContent = 'Connecting to ATEM at ' + autoConnectIP;
            }

            setTimeout(function () {
                connectToAtem(new Event('click'));
            }, 500);
        } else {
            document.getElementById('ipAddress').focus();
        }
    });

    // Suggestion dropdown wiring: filter as the operator types; arrows move
    // the highlight, Enter picks it (or submits a literal IP), Escape/blur
    // close the list.
    var ipInput = document.getElementById('ipAddress');

    ipInput.addEventListener('input', function () {
        suggestIndex = 0;
        renderSuggestions();
    });

    ipInput.addEventListener('blur', function () {
        hideSuggestions();
    });

    ipInput.addEventListener('keydown', function (e) {
        var listOpen = !document.getElementById('ipSuggestions').classList.contains('hidden');
        var matches = listOpen ? equipmentMatches(ipInput.value).slice(0, 8) : [];

        if (e.key === 'ArrowDown' && matches.length) {
            e.preventDefault();
            suggestIndex = (suggestIndex + 1) % matches.length;
            renderSuggestions();
        } else if (e.key === 'ArrowUp' && matches.length) {
            e.preventDefault();
            suggestIndex = (suggestIndex - 1 + matches.length) % matches.length;
            renderSuggestions();
        } else if (e.key === 'Escape') {
            hideSuggestions();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            // Literal IPs connect as typed; otherwise the highlighted
            // suggestion wins; otherwise let resolveTarget explain.
            if (!IP_SHAPE.test(ipInput.value.trim()) && matches.length) {
                selectSuggestion(matches[suggestIndex]);
            } else {
                connectToAtem(e);
            }
        }
    });
})();
