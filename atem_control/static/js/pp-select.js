/**
 * pp-select — styled popover facade over native <select> elements.
 *
 * Tag any select with data-pp-select and it gets the app's anchored-popover
 * look (same language as the equipment location picker) while the native
 * select stays in the DOM as an invisible overlay: form value, required-
 * validation bubbles, inline onchange= handlers and htmx
 * hx-trigger="change" bindings all keep working via a dispatched change
 * event. Re-enhances automatically after every htmx swap.
 *
 * Framework-driven selects (e.g. Alpine :value bindings on the ATEM
 * control page) are supported: a light sync loop keeps the facade label
 * and disabled state mirroring the native select, and the option panel is
 * rebuilt from the live options on every open (x-for/dynamic options
 * work). Still NOT for selects inside JS-cloned template rows — clones
 * lose the facade's listeners.
 *
 * Loaded globally from base.html (defer). Styles: pp-theme.css .pp-select-*.
 */

(function () {
    'use strict';

    function closeAllPPSelects(except) {
        document.querySelectorAll('.pp-select-panel').forEach(function (p) {
            if (p !== except) p.classList.add('hidden');
        });
        document.querySelectorAll('.pp-select-trigger .pp-select-chevron').forEach(function (c) {
            if (!except || !except.parentNode.contains(c)) c.classList.remove('rotate-180');
        });
    }

    function enhancePPSelects(root) {
        (root || document).querySelectorAll('select[data-pp-select]').forEach(function (sel) {
            if (sel.closest('.pp-select')) return; // already enhanced

            var wrap = document.createElement('div');
            wrap.className = 'pp-select w-full';
            sel.parentNode.insertBefore(wrap, sel);

            var trigger = document.createElement('button');
            trigger.type = 'button';
            trigger.className = 'select select-bordered w-full flex items-center justify-between pr-3 pp-select-trigger';
            // Carry the native select's size/text classes onto the trigger
            ['select-xs', 'select-sm', 'select-lg', 'text-sm', 'text-xs', 'font-mono'].forEach(function (cls) {
                if (sel.classList.contains(cls)) trigger.classList.add(cls);
            });
            trigger.disabled = sel.disabled;
            var label = document.createElement('span');
            label.className = 'truncate text-left flex-1';
            var chevron = document.createElement('i');
            chevron.className = 'bi bi-chevron-down text-xs transition-transform duration-200 pp-select-chevron';
            trigger.appendChild(label);
            trigger.appendChild(chevron);

            var panel = document.createElement('div');
            panel.className = 'pp-select-panel hidden bg-base-200 rounded-lg shadow-lg border border-base-content/10';

            function currentText() {
                var opt = sel.options[sel.selectedIndex];
                return opt ? opt.textContent.trim() : '';
            }

            function renderLabel() {
                var text = currentText();
                label.textContent = text;
                label.classList.toggle('text-base-content/50', !sel.value);
            }

            function rebuildOptions() {
                panel.innerHTML = '';
                Array.prototype.forEach.call(sel.options, function (opt) {
                    var row = document.createElement('div');
                    row.className = 'pp-select-option' + (opt.selected ? ' selected' : '');
                    var text = document.createElement('span');
                    text.className = 'truncate';
                    text.textContent = opt.textContent.trim();
                    row.appendChild(text);
                    if (opt.selected) {
                        var check = document.createElement('i');
                        check.className = 'bi bi-check-lg';
                        row.appendChild(check);
                    }
                    row.addEventListener('click', function () {
                        sel.value = opt.value;
                        renderLabel();
                        panel.classList.add('hidden');
                        chevron.classList.remove('rotate-180');
                        // htmx (hx-trigger="change") and any listeners fire
                        // exactly as if the native select had been used.
                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                    });
                    panel.appendChild(row);
                });
            }

            trigger.addEventListener('click', function () {
                var opening = panel.classList.contains('hidden');
                closeAllPPSelects(opening ? panel : null);
                if (!opening) {
                    panel.classList.add('hidden');
                    chevron.classList.remove('rotate-180');
                    return;
                }
                rebuildOptions();
                panel.classList.remove('hidden');
                chevron.classList.add('rotate-180');
                // Flip above the trigger when the viewport lacks room below
                var rect = trigger.getBoundingClientRect();
                var panelH = Math.min(window.innerHeight * 0.4, 280) + 16;
                var flipUp = (window.innerHeight - rect.bottom < panelH)
                          && (rect.top > panelH);
                panel.classList.toggle('drop-up', flipUp);
            });

            // Keep the facade label honest if something else fires change
            // on the native select (e.g. another script setting the value).
            sel.addEventListener('change', renderLabel);

            sel.tabIndex = -1;
            wrap.appendChild(sel);
            wrap.appendChild(trigger);
            wrap.appendChild(panel);
            renderLabel();
            // Registered for the sync loop: frameworks (Alpine :value) set
            // the value property without firing change, and may toggle
            // disabled — mirror both onto the facade.
            wrap._ppSync = function () {
                var text = currentText();
                if (label.textContent !== text) renderLabel();
                if (trigger.disabled !== sel.disabled) trigger.disabled = sel.disabled;
            };
        });
    }
    window.enhancePPSelects = enhancePPSelects;

    document.addEventListener('click', function (event) {
        if (!event.target.closest('.pp-select')) closeAllPPSelects();
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closeAllPPSelects();
    });
    document.addEventListener('DOMContentLoaded', function () { enhancePPSelects(); });
    document.body.addEventListener('htmx:afterSwap', function (evt) {
        enhancePPSelects(evt.detail.target);
    });
    setInterval(function () {
        // Pick up selects rendered after load (Alpine x-for accordions on
        // the ATEM page render once switcher state arrives), then keep
        // every facade's label/disabled state mirroring its native select.
        enhancePPSelects();
        document.querySelectorAll('.pp-select').forEach(function (wrap) {
            if (wrap._ppSync) wrap._ppSync();
        });
    }, 750);
})();
