/* ATEM profile save/load — section-selection dialog + file handling
 * for the control page.
 *
 *   saveProfileFiles(basename, xml, imageManifest, ip)
 *     Sequential <a download> per file. Browser shows a "this site
 *     wants to download multiple files" prompt once; everything lands
 *     in Downloads.
 *
 *   loadProfileFiles({xmlOnly|imageOnly})
 *     Standard <input type="file"> pickers — single-select for the
 *     XML, multi-select for the matching media-pool images.
 *
 * The File System Access API path that briefly shipped here was
 * removed because Chrome blocks ``showDirectoryPicker`` access to
 * Downloads (the natural location for the fallback path's output) and
 * to other system folders, making it actively worse than just
 * downloading. The downloads path works in every browser, no folder
 * restrictions.
 */
(function () {
    'use strict';

    // ---------- helpers ----------

    function csrf() {
        const el = document.querySelector('[name=csrfmiddlewaretoken]');
        return el ? el.value : '';
    }

    function _newSessionId() {
        // crypto.randomUUID is available on every browser the studio
        // supports (Chrome 92+, Firefox 95+, Safari 15.4+). Falls back
        // to a Math.random-based UUIDv4 for older targets.
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return ('xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx').replace(/[xy]/g, function (c) {
            const r = Math.random() * 16 | 0;
            const v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    }

    function _humanizeError(e, action) {
        // Translate generic JS exceptions into operator-friendly text
        // shown in the dialog's result panel.
        if (!e) return (action || 'Operation') + ' failed';
        if (e.name === 'NotFoundError') {
            return 'A required file or folder was not found: '
                + (e.message || '');
        }
        if (e.message) return e.message;
        return String(e);
    }

    function flatSections(descriptor) {
        // The descriptor's ``sections`` is already flat. Each section
        // carries a ``group`` field (switcher_me / switcher_global /
        // media / io / others) that the renderer uses to lay things
        // out; this helper just returns the array for bulk-toggle and
        // selection-map building.
        return (descriptor && descriptor.sections) || [];
    }

    function sectionsInGroup(descriptor, groupName) {
        return flatSections(descriptor).filter(s => s.group === groupName);
    }

    function sectionsInMeTab(descriptor, meIndex) {
        // meIndex is the 0-based M/E index (me_tabs[].index /
        // sections[].me_tab in the descriptor).
        return flatSections(descriptor).filter(
            s => s.group === 'switcher_me' && s.me_tab === meIndex);
    }

    // Expose for Alpine templates so the rendering can ask the
    // descriptor directly which sections belong where. Each entry is
    // {index, available, reason} — always 4 (ATEM platform ceiling);
    // unavailable tabs render greyed/inert.
    function _meTabs(descriptor) {
        return (descriptor && descriptor.me_tabs) || [];
    }

    // One me_tabs entry by M/E index (or null) — used by the template
    // to render the active tab's panel / unavailable-reason text.
    function meTab(descriptor, meIndex) {
        return _meTabs(descriptor).find(t => t.index === meIndex) || null;
    }

    function _selectableMeSections(descriptor, meIndex) {
        return sectionsInMeTab(descriptor, meIndex).filter(s => s.supported);
    }

    // Per-tab master checkbox: keep the element's checked/indeterminate
    // in sync with that M/E's selectable sections. Called from an
    // x-effect so it re-runs whenever profileSelections changes —
    // checked = all selected, indeterminate = some, unchecked = none.
    function syncMeTabCheckbox(el, descriptor, selections, meIndex) {
        const avail = _selectableMeSections(descriptor, meIndex);
        const n = avail.filter(s => !!selections[s.id]).length;
        el.checked = avail.length > 0 && n === avail.length;
        el.indeterminate = n > 0 && n < avail.length;
    }

    // Toggle every selectable (non-greyed) section in one M/E tab.
    function toggleMeTab(descriptor, selections, meIndex, value) {
        for (const s of _selectableMeSections(descriptor, meIndex)) {
            selections[s.id] = !!value;
        }
    }

    function _firstAvailableMeTab(descriptor) {
        const t = _meTabs(descriptor).find(tab => tab.available);
        return t ? t.index : 0;
    }

    function defaultSelections(descriptor) {
        // Build the initial selection map from descriptor's `checked` flags,
        // forcing False for unsupported sections.
        const sel = {};
        for (const s of flatSections(descriptor)) {
            sel[s.id] = !!(s.supported && s.checked);
        }
        return sel;
    }

    function selectionsForBackend(descriptor, sel) {
        // Translate the dialog's selection map (per-section ids) into the
        // shape the backend's _build_save_options / _build_apply_options
        // expect. 1:1 passthrough — the descriptor is flat.
        const out = {};
        for (const s of flatSections(descriptor)) {
            out[s.id] = !!sel[s.id];
        }
        return out;
    }

    // ---------- Dialog open helpers ----------

    // Captured at open time (L25): the IP the dialog's descriptor was
    // fetched against, and the dialog's Alpine scope (for forceClose).
    // Only one dialog can be open at a time; both are refreshed on every
    // open, so staleness after a normal close is harmless.
    let _dialogIp = null;
    let _dialogAlpine = null;

    async function openSaveDialog(ip, alpine) {
        if (!ip) return;
        _dialogIp = ip;
        _dialogAlpine = alpine;
        alpine.profileDialogMode = 'save';
        alpine.profileDialogTitle = 'Save Switcher State';
        alpine.profileBusy = false;
        alpine.profileBusyMessage = '';
        alpine.profileDescriptor = null;
        alpine.profileSelections = {};
        alpine.profileApplyResult = null;
        alpine.profilePendingXml = null;
        alpine.profilePendingImages = null;
        alpine.profileDialogOpen = true;

        // Fetch section descriptor
        try {
            const r = await fetch(
                '/atem/profile/save_dialog_init/?ip=' + encodeURIComponent(ip));
            const data = await r.json();
            if (!r.ok) {
                alpine.profileApplyResult = {error: data.error || 'failed to load'};
                return;
            }
            alpine.profileDescriptor = data;
            alpine.profileSelections = defaultSelections(data);
            alpine.profileActiveMeTab = _firstAvailableMeTab(data);
        } catch (e) {
            alpine.profileApplyResult = {error: _humanizeError(e, 'Save')};
        }
    }

    async function openLoadDialog(ip, alpine) {
        if (!ip) return;
        _dialogIp = ip;
        _dialogAlpine = alpine;
        alpine.profileDialogMode = 'load';
        alpine.profileDialogTitle = 'Load Switcher State';
        alpine.profileBusy = false;
        alpine.profileBusyMessage = '';
        alpine.profileDescriptor = null;
        alpine.profileSelections = {};
        alpine.profileApplyResult = null;
        alpine.profilePendingXml = null;
        alpine.profilePendingImages = null;
        alpine.profileDialogOpen = true;

        // The load flow needs an XML up front. Pick it via the file
        // picker, upload to /load_xml, render the descriptor.
        try {
            const picked = await loadProfileFiles({xmlOnly: true});
            if (!picked || !picked.xmlFile) {
                // Operator dismissed the file picker — close the dialog.
                alpine.profileDialogOpen = false;
                return;
            }
            alpine.profilePendingXml = picked.xmlFile;

            const fd = new FormData();
            // The load descriptor greys M/E tabs by the connected
            // switcher's topology as well as the file contents.
            fd.append('ip', ip);
            fd.append('profile', picked.xmlFile);
            const r = await fetch('/atem/profile/load_xml/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrf()},
                body: fd,
            });
            const data = await r.json();
            if (!r.ok) {
                alpine.profileApplyResult = {error: data.error || 'parse failed'};
                return;
            }
            alpine.profileDescriptor = data;
            alpine.profileSelections = defaultSelections(data);
            alpine.profileActiveMeTab = _firstAvailableMeTab(data);
        } catch (e) {
            alpine.profileApplyResult = {error: _humanizeError(e, 'Load')};
        }
    }

    function selectAll(selections, descriptor, value) {
        for (const s of flatSections(descriptor)) {
            if (s.supported) {
                selections[s.id] = !!value;
            }
        }
    }

    // ---------- File handling ----------

    /**
     * Deliver a saved profile to the operator as a single download.
     * The backend returns either a bare XML response or a ZIP bundling
     * the XML + ATEM Media Pool/ subfolder; this function just hands
     * the blob to the browser via a single <a download> click.
     *
     * @param {Blob}   blob     response body from /atem/profile/save/
     * @param {string} filename name to use in the download (parsed
     *                          from Content-Disposition by the caller)
     * @returns {Promise<{kind: string, message: string}>}
     */
    async function saveProfileFiles(blob, filename) {
        const url = URL.createObjectURL(blob);
        try {
            await triggerDownload(url, filename);
        } finally {
            // Revoke a moment later — too-eager revoke kills the download
            // in some browsers.
            setTimeout(() => URL.revokeObjectURL(url), 5000);
        }
        const isZip = filename.toLowerCase().endsWith('.zip');
        return {
            kind: isZip ? 'zip' : 'xml',
            message: isZip
                ? 'Saved ' + filename + ' to your Downloads folder. '
                  + 'Extract the ZIP to get the XML and the '
                  + '"ATEM Media Pool" folder of images.'
                : 'Saved ' + filename + ' to your Downloads folder.',
        };
    }

    /**
     * Pick the profile XML and (optionally) the media-pool image files
     * the operator wants to upload. Drives standard <input type="file">
     * pickers — single-select for the XML, multi-select for images.
     *
     * @param {object} opts
     * @param {boolean} opts.xmlOnly   pick only the XML
     * @param {boolean} opts.imageOnly pick only the images
     * @returns {Promise<{xmlFile?: File, imageFiles?: File[]}>}
     */
    async function loadProfileFiles(opts) {
        opts = opts || {};
        if (opts.xmlOnly) {
            const xmlFile = await pickOneFile('.xml,application/xml,text/xml');
            return {xmlFile: xmlFile};
        }
        if (opts.imageOnly) {
            const imageFiles = await pickManyFiles(
                'image/png,image/jpeg,image/*');
            return {imageFiles: imageFiles};
        }
        // Default: both, sequentially. Used by direct callers; not
        // exercised by the section-selection dialog.
        const xmlFile = await pickOneFile('.xml,application/xml,text/xml');
        if (!xmlFile) return null;
        const imageFiles = await pickManyFiles(
            'image/png,image/jpeg,image/*');
        return {xmlFile: xmlFile, imageFiles: imageFiles};
    }

    function pickOneFile(accept) {
        return new Promise(function (resolve) {
            const inp = document.createElement('input');
            inp.type = 'file';
            inp.accept = accept || '';
            inp.style.display = 'none';
            document.body.appendChild(inp);
            inp.addEventListener('change', function () {
                const f = inp.files && inp.files[0] || null;
                document.body.removeChild(inp);
                resolve(f);
            });
            // If the operator dismisses without picking, the change event
            // may never fire — Chrome calls 'cancel' but Firefox/Safari
            // don't. Use a focus-back fallback to detect dismissal.
            const dismissed = function () {
                setTimeout(function () {
                    if (inp.parentNode) {
                        document.body.removeChild(inp);
                        resolve(null);
                    }
                    window.removeEventListener('focus', dismissed);
                }, 300);
            };
            window.addEventListener('focus', dismissed);
            inp.click();
        });
    }

    function pickManyFiles(accept) {
        return new Promise(function (resolve) {
            const inp = document.createElement('input');
            inp.type = 'file';
            inp.multiple = true;
            inp.accept = accept || '';
            inp.style.display = 'none';
            document.body.appendChild(inp);
            inp.addEventListener('change', function () {
                const arr = Array.from(inp.files || []);
                document.body.removeChild(inp);
                resolve(arr);
            });
            const dismissed = function () {
                setTimeout(function () {
                    if (inp.parentNode) {
                        document.body.removeChild(inp);
                        resolve([]);
                    }
                    window.removeEventListener('focus', dismissed);
                }, 300);
            };
            window.addEventListener('focus', dismissed);
            inp.click();
        });
    }

    function triggerDownload(url, filename) {
        return new Promise(function (resolve) {
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            a.style.display = 'none';
            document.body.appendChild(a);
            a.click();
            // Remove on next tick to give the browser time to process.
            setTimeout(function () {
                if (a.parentNode) {
                    document.body.removeChild(a);
                }
                resolve();
            }, 50);
        });
    }

    function _filenameFromContentDisposition(header, fallback) {
        // Prefer RFC 5987 ``filename*=UTF-8''<encoded>``; fall back to
        // the bare ``filename="..."`` form. Spaces and slashes already
        // sanitised on the backend (_slug_filename).
        if (!header) return fallback;
        const star = header.match(/filename\*=UTF-8''([^;]+)/i);
        if (star) {
            try { return decodeURIComponent(star[1]); } catch (_e) { /* fall through */ }
        }
        const quoted = header.match(/filename="([^"]+)"/i);
        if (quoted) return quoted[1];
        const bare = header.match(/filename=([^;]+)/i);
        if (bare) return bare[1].trim();
        return fallback;
    }

    // ---------- Run-the-dialog handlers ----------

    async function runDialog(ip, alpine) {
        // L25: the descriptor + selections were fetched against the IP
        // captured at open time — prefer it over the click-time store
        // read so an in-page ATEM switch with the dialog open can't
        // apply one ATEM's selections to another.
        const targetIp = _dialogIp || ip;
        if (alpine.profileDialogMode === 'save') {
            return runSave(targetIp, alpine);
        }
        return runLoad(targetIp, alpine);
    }

    // Close the dialog from outside its Alpine scope. Called by
    // ATEMControl.connect() on an in-page ATEM switch (L25): the open
    // dialog's descriptor/selections target the previous ATEM.
    function forceClose() {
        if (_dialogAlpine) {
            _dialogAlpine.profileDialogOpen = false;
        }
        _dialogIp = null;
        _dialogAlpine = null;
    }

    async function runSave(ip, alpine) {
        if (!ip || !alpine.profileDescriptor) return;
        alpine.profileBusy = true;
        alpine.profileApplyResult = null;
        // Constant status throughout — capture progress is communicated
        // by the progress bar at the top of the dialog, so the footer
        // status only needs to say "things are happening".
        alpine.profileBusyMessage = 'Creating XML file…';

        // Per-save session id — links the in-flight POST to the
        // WS-side message handler so it knows which progress events
        // belong to this dialog (vs. a stale prior save).
        // Capture state lives on Alpine.store('atem'), not the page
        // x-data, so the WS handler in atem_control.js (which only
        // sees the store) writes the same object the modal template
        // reads via $store.atem.profileCapture*.
        const store = window.Alpine && window.Alpine.store
            ? window.Alpine.store('atem') : null;
        const sessionId = _newSessionId();
        if (store) {
            store.profileCaptureSession = sessionId;
            store.profileCaptureProgress = null;
        }

        try {
            const sections = selectionsForBackend(
                alpine.profileDescriptor, alpine.profileSelections);

            const r = await fetch(
                '/atem/profile/save/?ip=' + encodeURIComponent(ip), {
                method: 'POST',
                headers: {'X-CSRFToken': csrf(),
                          'Content-Type': 'application/json',
                          'X-Capture-Session': sessionId},
                body: JSON.stringify({sections: sections}),
            });
            if (!r.ok) {
                // Errors come back as JSON; success bodies are XML or ZIP.
                let err = 'save failed';
                try {
                    const data = await r.json();
                    err = data.error || err;
                } catch (_e) { /* non-JSON error body */ }
                alpine.profileApplyResult = {error: err};
                return;
            }

            const filename = _filenameFromContentDisposition(
                r.headers.get('Content-Disposition'), 'atem-profile.xml');
            const blob = await r.blob();
            const out = await saveProfileFiles(blob, filename);

            alpine.profileApplyResult = {
                applied: ['profile (' + filename + ')'],
                savedFiles: out.message,
            };
        } catch (e) {
            alpine.profileApplyResult = {error: _humanizeError(e, 'Save')};
        } finally {
            alpine.profileBusy = false;
            alpine.profileBusyMessage = '';
            if (store) {
                store.profileCaptureSession = '';
                store.profileCaptureProgress = null;
            }
        }
    }

    // L10: cancel an in-flight save's media-pool capture. Fire-and-forget
    // POST carrying the same session id; the backend stops at the next slot
    // boundary and the save's own response then returns the partial ZIP.
    async function cancelSave(alpine) {
        const store = window.Alpine && window.Alpine.store
            ? window.Alpine.store('atem') : null;
        const sessionId = store && store.profileCaptureSession;
        if (!sessionId) return;
        alpine.profileBusyMessage = 'Cancelling…';
        try {
            await fetch('/atem/profile/save/cancel/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrf(),
                          'Content-Type': 'application/json',
                          'X-Capture-Session': sessionId},
                body: JSON.stringify({session_id: sessionId}),
            });
        } catch (_e) { /* best-effort; the save still returns on its own */ }
    }

    async function runLoad(ip, alpine) {
        if (!ip || !alpine.profileDescriptor || !alpine.profilePendingXml) return;
        alpine.profileBusy = true;
        alpine.profileApplyResult = null;
        alpine.profileBusyMessage = 'Applying profile…';
        try {
            const sections = selectionsForBackend(
                alpine.profileDescriptor, alpine.profileSelections);

            // Decide whether to ask the operator for image files. The
            // load XML's <Still> entries (descriptor.referenced_images)
            // tell us which filenames to expect.
            const wantImages = !!sections.media_pool_images;
            const referenced = (alpine.profileDescriptor.referenced_images || []);
            let imageFiles = [];
            if (wantImages && referenced.length) {
                alpine.profileBusyMessage = 'Pick the matching media-pool image files…';
                const picked = await loadProfileFiles({imageOnly: true});
                imageFiles = (picked && picked.imageFiles) || [];
                alpine.profileBusyMessage = 'Applying profile…';
            }

            const fd = new FormData();
            fd.append('ip', ip);
            fd.append('profile', alpine.profilePendingXml);
            fd.append('sections', JSON.stringify(sections));
            for (let i = 0; i < imageFiles.length; i += 1) {
                fd.append('image_' + i, imageFiles[i]);
            }
            const r = await fetch('/atem/profile/load/', {
                method: 'POST',
                headers: {'X-CSRFToken': csrf()},
                body: fd,
            });
            const data = await r.json();
            if (!r.ok) {
                alpine.profileApplyResult = {error: data.error || 'apply failed'};
                return;
            }
            alpine.profileApplyResult = data;
        } catch (e) {
            alpine.profileApplyResult = {error: _humanizeError(e, 'Load')};
        } finally {
            alpine.profileBusy = false;
            alpine.profileBusyMessage = '';
        }
    }

    // ---------- Public surface ----------

    window.AtemProfile = {
        openSaveDialog: openSaveDialog,
        openLoadDialog: openLoadDialog,
        forceClose: forceClose,
        selectAll: selectAll,
        runDialog: runDialog,
        cancelSave: cancelSave,
        saveProfileFiles: saveProfileFiles,
        loadProfileFiles: loadProfileFiles,
        // Layout helpers consumed by the Alpine template.
        sectionsInGroup: sectionsInGroup,
        sectionsInMeTab: sectionsInMeTab,
        meTabs: _meTabs,
        meTab: meTab,
        syncMeTabCheckbox: syncMeTabCheckbox,
        toggleMeTab: toggleMeTab,
    };
})();
