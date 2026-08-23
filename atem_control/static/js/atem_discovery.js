/**
 * ATEM network discovery — the "On your network" section of the Connect
 * page. Two sources, one unified list:
 *
 *   - Passive mDNS (polled from /atem/api/discovered/): ATEMs that
 *     announce themselves over Bonjour. Zero switcher contact.
 *   - Opt-in subnet scan (/atem/api/scan/): a light hello-sweep of the
 *     server's own /24 for ATEMs that don't advertise.
 *
 * WebATEM-owned (atem_connect.js is synced verbatim from the parent
 * project). Integrates only through that file's public globals
 * window.setIP() and window.connectToAtem().
 */
(function () {
    'use strict';

    var section = document.getElementById('discoverySection');
    if (!section) return;

    var listEl = document.getElementById('discoveryList');
    var emptyEl = document.getElementById('discoveryEmpty');
    var scanBtn = document.getElementById('scanBtn');

    // ip -> {ip, name, model, source}. mDNS entries win over scan-only
    // ones (they carry a name), so a later scan never downgrades a name.
    var seen = {};

    function merge(atems) {
        var changed = false;
        atems.forEach(function (a) {
            var prev = seen[a.ip];
            if (!prev || (prev.source === 'scan' && a.source === 'mdns') ||
                (a.name && !prev.name)) {
                seen[a.ip] = a;
                changed = true;
            }
        });
        return changed;
    }

    function ipSortKey(ip) {
        return ip.split('.').map(function (o) {
            return ('00' + o).slice(-3);
        }).join('.');
    }

    function render() {
        var items = Object.keys(seen).map(function (ip) { return seen[ip]; });
        items.sort(function (a, b) {
            var an = (a.name || '').toLowerCase(), bn = (b.name || '').toLowerCase();
            if (an && bn) return an < bn ? -1 : an > bn ? 1 : 0;
            if (an) return -1;
            if (bn) return 1;
            return ipSortKey(a.ip) < ipSortKey(b.ip) ? -1 : 1;
        });

        listEl.innerHTML = '';
        if (!items.length) {
            emptyEl.classList.remove('hidden');
            return;
        }
        emptyEl.classList.add('hidden');

        items.forEach(function (a) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-sm btn-outline btn-primary justify-start gap-2 normal-case';

            var icon = document.createElement('i');
            icon.className = 'bi bi-hdd-network';
            btn.appendChild(icon);

            var label = document.createElement('span');
            label.className = 'flex flex-col items-start leading-tight';
            var top = document.createElement('span');
            top.className = 'font-medium';
            top.textContent = a.name || a.ip;   // textContent — names are device-supplied
            label.appendChild(top);
            var sub = document.createElement('span');
            sub.className = 'text-[10px] opacity-60 font-mono';
            sub.textContent = a.name ? a.ip : (a.model || 'ATEM');
            label.appendChild(sub);
            btn.appendChild(label);

            // One-click connect, ASC-style: prefill + fire the existing
            // connect flow.
            btn.addEventListener('click', function () {
                if (typeof window.setIP === 'function') window.setIP(a.ip);
                if (typeof window.connectToAtem === 'function') {
                    window.connectToAtem(new Event('submit'));
                }
            });
            listEl.appendChild(btn);
        });
    }

    function pollDiscovered() {
        fetch('/atem/api/discovered/', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.atems && merge(data.atems)) render();
            })
            .catch(function () { /* transient; next poll retries */ });
    }

    function runScan() {
        scanBtn.disabled = true;
        var original = scanBtn.innerHTML;
        scanBtn.innerHTML = '<span class="loading loading-spinner loading-xs"></span> Scanning…';
        fetch('/atem/api/scan/', { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.atems) { merge(data.atems); render(); }
            })
            .catch(function () { /* ignore */ })
            .finally(function () {
                scanBtn.disabled = false;
                scanBtn.innerHTML = original;
            });
    }

    if (scanBtn) scanBtn.addEventListener('click', runScan);

    // Poll mDNS immediately (starts the listener server-side) and keep it
    // fresh while the Connect page is open.
    pollDiscovered();
    setInterval(pollDiscovered, 3000);
})();
