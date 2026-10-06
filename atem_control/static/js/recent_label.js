/**
 * Recent connections by name or by IP — the viewer's choice, made with the
 * Name / IP switch beside "Recent Connections" on the Connect page and kept in
 * this browser, so each device decides for itself. The Recent menu in the
 * control page header follows the same choice.
 *
 * Loaded in <head> (not deferred): the attribute is on <html> before the list
 * paints, so a page set to IP never flashes the names first. The CSS that
 * acts on it is in brand.css (.recent-by-name / .recent-by-ip). Without this
 * script, or with storage blocked, the list reads by name, as it always has.
 */
(function () {
    'use strict';

    var KEY = 'webatem_recent_label';
    var root = document.documentElement;

    function read() {
        try {
            return localStorage.getItem(KEY) === 'ip' ? 'ip' : 'name';
        } catch (e) {
            return 'name';
        }
    }

    function apply(mode) {
        root.setAttribute('data-recent-label', mode);
        var buttons = document.querySelectorAll('[data-recent-label-set]');
        for (var i = 0; i < buttons.length; i++) {
            var on = buttons[i].getAttribute('data-recent-label-set') === mode;
            buttons[i].setAttribute('aria-pressed', on ? 'true' : 'false');
        }
    }

    apply(read());
    document.addEventListener('DOMContentLoaded', function () { apply(read()); });

    document.addEventListener('click', function (e) {
        var button = e.target.closest && e.target.closest('[data-recent-label-set]');
        if (!button) return;
        var mode = button.getAttribute('data-recent-label-set') === 'ip' ? 'ip' : 'name';
        try {
            localStorage.setItem(KEY, mode);
        } catch (err) {
            /* storage blocked (a private window): this page only */
        }
        apply(mode);
    });
})();
