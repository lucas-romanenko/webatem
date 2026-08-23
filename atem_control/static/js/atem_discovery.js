/**
 * ATEM network discovery — the "On your network" section of the Connect
 * page. Auto-discovery by default, two sources merged into one list:
 *
 *   - Passive mDNS (polled from /atem/api/discovered/): ATEMs that
 *     announce themselves over Bonjour, with their names. Zero switcher
 *     contact.
 *   - Automatic subnet sweep (/atem/api/scan/): a light hello-sweep that
 *     runs on load — no button press — so ATEMs that don't advertise
 *     still show up. It sweeps the host's own /24 AND any subnet where
 *     mDNS spots an ATEM (so a switcher seen on a different segment pulls
 *     that segment in too). Half-open only: no session on any switcher.
 *
 * A sweep gives an IP; the name only exists after mDNS or a real connect,
 * so sweep-only entries show their IP and resolve their name when clicked.
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
    var subnetInput = document.getElementById('scanSubnet');

    // ip -> {ip, name, model, source}. mDNS entries win over scan-only
    // ones (they carry a name), so a later scan never downgrades a name.
    var seen = {};
    var localSubnet = null;

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
        return ip.split('.').map(function (o) { return ('00' + o).slice(-3); }).join('.');
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
                if (!data) return;
                // Prefill the manual-scan field with the host's own subnet
                // (used only for the opt-in "Scan subnet" fallback).
                if (data.subnet && !localSubnet) {
                    localSubnet = data.subnet;
                    if (subnetInput && !subnetInput.value) subnetInput.value = localSubnet;
                }
                if (data.atems && data.atems.length && merge(data.atems)) render();
            })
            .catch(function () { /* transient; next poll retries */ });
    }

    // Opt-in sweep of a subnet — the fallback for ATEMs that don't
    // advertise mDNS (rare). Finds them by IP; names still come from mDNS
    // where available.
    function manualScan() {
        var subnet = (subnetInput && subnetInput.value.trim()) || localSubnet;
        if (!subnet) return;
        scanBtn.disabled = true;
        var original = scanBtn.innerHTML;
        scanBtn.innerHTML = '<span class="loading loading-spinner loading-xs"></span>';
        var restore = function () { scanBtn.disabled = false; scanBtn.innerHTML = original; };
        fetch('/atem/api/scan/?subnet=' + encodeURIComponent(subnet), { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.atems) { merge(data.atems); render(); }
            })
            .catch(function () { /* ignore */ })
            .finally(restore);
    }

    if (scanBtn) scanBtn.addEventListener('click', manualScan);
    if (subnetInput) {
        subnetInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); manualScan(); }
        });
    }

    // Poll mDNS immediately (starts the listener + auto-sweeps) and keep
    // it fresh while the Connect page is open.
    pollDiscovered();
    setInterval(pollDiscovered, 3000);
})();
