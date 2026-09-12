/**
 * Server settings dialog on the connect page — where this WebATEM listens
 * (address + port) and Start at login, like Companion's launcher window.
 * Talks to /server/settings/ (webatem/views.py). When the launcher hosts
 * the server it restarts on the new address; this script then waits for
 * the new address to answer and sends the browser there.
 */
(function () {
    'use strict';

    var dialog = document.getElementById('serverSettings');
    if (!dialog) return;
    var hostSel = document.getElementById('listenHost');
    var portInp = document.getElementById('listenPort');
    var autoRow = document.getElementById('autostartRow');
    var autoTog = document.getElementById('autostartToggle');
    var note = document.getElementById('serverSettingsNote');
    var status = document.getElementById('serverSettingsStatus');
    var saveBtn = document.getElementById('serverSettingsSave');
    var ENDPOINT = '/server/settings/';
    var current = null;

    function csrf() {
        var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }
    function showStatus(text, cls) {
        status.textContent = text;
        status.className = 'text-sm ' + (cls || 'text-base-content/70');
        status.classList.remove('hidden');
    }
    function option(value, label) {
        var o = document.createElement('option');
        o.value = value; o.textContent = label;
        return o;
    }

    function render(d) {
        current = d;
        hostSel.innerHTML = '';
        hostSel.appendChild(option('0.0.0.0', 'All interfaces (recommended)'));
        (d.interfaces || []).forEach(function (e) {
            hostSel.appendChild(option(e.ip, e.ip === '127.0.0.1' ? 'This computer only (127.0.0.1)' : e.name + ' — ' + e.ip));
        });
        hostSel.value = d.host;
        if (hostSel.value !== d.host) {           // an address not in the list (env-set): show it
            hostSel.appendChild(option(d.host, d.host));
            hostSel.value = d.host;
        }
        portInp.value = d.port;
        autoRow.classList.toggle('hidden', !(d.autostart && d.autostart.available));
        autoTog.checked = !!(d.autostart && d.autostart.enabled);
        var locked = !d.restart_available;
        hostSel.disabled = locked; portInp.disabled = locked;
        if (locked) {
            note.textContent = d.source === 'env'
                ? 'This server was started with a fixed address (HOST / PORT in its environment). Change it there.'
                : 'This server is run by uvicorn directly, so the address is set where it was started.';
        } else {
            note.textContent = 'Saving restarts the server on the new address; open browsers reconnect.';
        }
        note.classList.remove('hidden');
        if (d.last_error) showStatus('Last attempt failed: ' + d.last_error, 'text-error');
        else status.classList.add('hidden');
    }

    function load() {
        showStatus('Loading…');
        fetch(ENDPOINT, { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(render)
            .catch(function () { showStatus('Could not read the server settings.', 'text-error'); });
    }

    function waitFor(url, done, fail) {
        var deadline = Date.now() + 30000;
        (function poll() {
            // An opaque (no-cors) response means something answered at the
            // new address; a network error means not yet.
            fetch(url, { mode: 'no-cors', cache: 'no-store' })
                .then(function () { done(); })
                .catch(function () {
                    if (Date.now() > deadline) fail(); else setTimeout(poll, 500);
                });
        })();
    }

    function save() {
        var body = { host: hostSel.value, port: parseInt(portInp.value, 10) };
        if (!autoRow.classList.contains('hidden')) body.autostart = autoTog.checked;
        saveBtn.disabled = true;
        showStatus('Saving…');
        fetch(ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
            body: JSON.stringify(body)
        })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
                saveBtn.disabled = false;
                if (!res.ok) { showStatus(res.d.error || 'Not saved.', 'text-error'); return; }
                var d = res.d;
                if (d.autostart_error) showStatus('Saved, but Start at login could not be changed: ' + d.autostart_error, 'text-warning');
                if (d.restarting) {
                    showStatus('Restarting on ' + d.url + ' …');
                    var target = new URL(d.url);
                    var reachable = target.hostname !== '127.0.0.1' || ['127.0.0.1', 'localhost'].indexOf(location.hostname) !== -1;
                    waitFor(d.url, function () {
                        if (reachable) location.href = d.url;
                        else showStatus('Now listening at ' + d.url + ' (open it from that computer).', 'text-success');
                    }, function () {
                        showStatus('The server did not come up at ' + d.url + ' within 30 s. It falls back to the previous address; reload to see it.', 'text-error');
                    });
                } else if (d.note) {
                    showStatus(d.note, 'text-warning');
                } else {
                    showStatus('Saved.', 'text-success');
                }
            })
            .catch(function () { saveBtn.disabled = false; showStatus('Not saved (network error).', 'text-error'); });
    }

    saveBtn.addEventListener('click', save);
    dialog.addEventListener('close', function () { status.classList.add('hidden'); });
    document.getElementById('serverSettingsBtn').addEventListener('click', load);
    // The tray's "Server settings…" item opens the page with ?settings=1.
    if (new URLSearchParams(location.search).get('settings') === '1') {
        dialog.showModal(); load();
    }
})();
