'use strict';
/*
 * custom-scrollbar.js — OUR OWN scrollbars, site-wide, both axes.
 *
 * Native scrollbar styling can't be made to stick on machines with OS
 * overlay/auto-hide scrollbars, so every scroller HIDES its native bar (CSS
 * in base.html) and gets a DOM overlay (.cbar / .cbar-thumb) that we fully
 * control: the same brand orange for every user on every OS, always visible
 * while there is something to scroll (so a sideways-scrolling panel like the
 * Audio mixer visibly says so), and NO reserved gutter, so content never
 * shifts when a bar appears (the ATEM settings drawer used to jump ~15px).
 *
 * Attaches automatically to <main> (the app-shell scroller) and to every
 * .overflow-y-auto / .overflow-y-scroll (vertical), .overflow-x-auto /
 * .overflow-x-scroll (horizontal) and .overflow-auto / .overflow-scroll
 * (both) element — those present at load and any added later (htmx swaps,
 * Alpine x-if/x-for, dialogs). data-cbar="x" | "y" | "xy" opts an element
 * in explicitly; data-cbar="off" opts it out (the CSS keeps its native bar).
 *
 * Each bar lives in the scroller's PARENT (made position:relative if it was
 * static) and is placed over the scroller's own box from offsetTop/Left —
 * LAYOUT coordinates, deliberately not getBoundingClientRect: rect maths
 * reads the scroller's box through any CSS transform on an ancestor, so a
 * panel scaling in (the Media Pool's scale-95 entrance) placed the bar
 * inset and it snapped into place at transitionend. Only the thumb is
 * interactive; the track lets clicks/wheel pass through.
 */
(function () {
    var V_CLASS = 'main, .overflow-y-auto, .overflow-y-scroll, .overflow-auto, .overflow-scroll';
    var H_CLASS = '.overflow-x-auto, .overflow-x-scroll, .overflow-auto, .overflow-scroll';
    var SELECTOR = V_CLASS + ', ' + H_CLASS + ', [data-cbar]';
    var BAR_W = 12, EDGE = 3, MIN_THUMB = 32;
    var attached = [];

    function axesFor(el) {
        if (!el || el.nodeType !== 1 || el._cbar) return null;
        var attr = el.getAttribute('data-cbar');
        if (attr === 'off') return null;
        var x = false, y = false;
        if (attr !== null) { y = attr === '' || attr.indexOf('y') >= 0; x = attr.indexOf('x') >= 0; }
        if (el.matches(V_CLASS)) y = true;
        if (el.matches(H_CLASS)) x = true;
        return (x || y) ? { x: x, y: y } : null;
    }

    function attach(scroller) {
        var axes = axesFor(scroller);
        if (!axes) return;
        var parent = scroller.parentElement;
        if (!parent) return;
        if (getComputedStyle(parent).position === 'static') parent.style.position = 'relative';

        var bars = {};
        function makeBar(axis) {
            var bar = document.createElement('div');
            bar.className = 'cbar' + (axis === 'x' ? ' cbar-x' : '');
            var thumb = document.createElement('div');
            thumb.className = 'cbar-thumb';
            bar.appendChild(thumb);
            parent.appendChild(bar);
            bars[axis] = { bar: bar, thumb: thumb, active: false };
            thumb.addEventListener('mousedown', function (e) { e.preventDefault(); startDrag(axis, e.clientX, e.clientY); });
            thumb.addEventListener('touchstart', function (e) { if (e.touches[0]) startDrag(axis, e.touches[0].clientX, e.touches[0].clientY); }, { passive: true });
        }
        if (axes.y) makeBar('y');
        if (axes.x) makeBar('x');
        scroller._cbar = bars;
        attached.push(scroller);

        var rafPending = false;

        function trackLen(axis) {
            // Leave the corner free when the other axis is showing a bar.
            var other = axis === 'y' ? bars.x : bars.y;
            var len = axis === 'y' ? scroller.clientHeight : scroller.clientWidth;
            return other && other.active ? len - BAR_W : len;
        }

        function renderAxis(axis) {
            var b = bars[axis];
            if (!b.bar.isConnected) parent.appendChild(b.bar);   // an innerHTML swap on the parent dropped it
            var sh = axis === 'y' ? scroller.scrollHeight : scroller.scrollWidth;
            var ch = axis === 'y' ? scroller.clientHeight : scroller.clientWidth;
            b.active = ch > 0 && sh > ch + 1;
            b.bar.classList.toggle('is-active', b.active);
            if (!b.active) return;
            // Layout coords: offsetTop/Left are relative to the offsetParent's
            // padding edge, which is where an absolute child positions too.
            var z = getComputedStyle(scroller).zIndex;
            b.bar.style.zIndex = z !== 'auto' ? String(parseInt(z, 10) + 1) : '';
            var track = trackLen(axis);
            var th = Math.max(MIN_THUMB, (ch / sh) * track);
            var pos = (track - th) * ((axis === 'y' ? scroller.scrollTop : scroller.scrollLeft) / (sh - ch));
            if (axis === 'y') {
                b.bar.style.top = scroller.offsetTop + 'px';
                b.bar.style.left = (scroller.offsetLeft + scroller.offsetWidth - BAR_W - EDGE) + 'px';
                b.bar.style.height = track + 'px';
                b.thumb.style.height = th + 'px';
                b.thumb.style.transform = 'translateY(' + pos + 'px)';
            } else {
                b.bar.style.left = scroller.offsetLeft + 'px';
                b.bar.style.top = (scroller.offsetTop + scroller.offsetHeight - BAR_W - EDGE) + 'px';
                b.bar.style.width = track + 'px';
                b.thumb.style.width = th + 'px';
                b.thumb.style.transform = 'translateX(' + pos + 'px)';
            }
        }

        function render() {
            rafPending = false;
            if (!scroller.isConnected) return;
            // Two passes so each axis sees the other's final active state
            // (the corner reservation depends on it).
            if (bars.y) renderAxis('y');
            if (bars.x) renderAxis('x');
            if (bars.y && bars.x) renderAxis('y');
        }

        function schedule() {
            if (rafPending) return;
            rafPending = true;
            requestAnimationFrame(render);
        }

        scroller.addEventListener('scroll', schedule, { passive: true });
        // Content-size changes that involve no node insertion — an accordion
        // section opening (x-collapse animates an inline height), an x-show
        // toggle, a class swap — are caught three ways: attribute mutations
        // (end state), transition/animation end events bubbling up from
        // descendants, and a ResizeObserver on the scroller's direct children
        // (mid-animation frames). All coalesce into one rAF render.
        var ro = window.ResizeObserver ? new ResizeObserver(schedule) : null;
        function observeChildren() {
            if (!ro) return;
            for (var c = scroller.firstElementChild; c; c = c.nextElementSibling) ro.observe(c);
        }
        if (ro) { ro.observe(scroller); ro.observe(parent); observeChildren(); }
        if (window.MutationObserver) {
            new MutationObserver(function (muts) {
                for (var i = 0; i < muts.length; i++) {
                    if (muts[i].type === 'childList' && muts[i].target === scroller) { observeChildren(); break; }
                }
                schedule();
            }).observe(scroller, {
                childList: true, subtree: true, characterData: true,
                attributes: true, attributeFilter: ['style', 'class', 'hidden', 'open'],
            });
        }
        scroller.addEventListener('transitionend', schedule);
        scroller.addEventListener('animationend', schedule);
        window.addEventListener('resize', schedule);
        // htmx swaps content into scrollers → scroll size changes
        document.body.addEventListener('htmx:afterSettle', schedule);

        // Drag the thumb to scroll.
        function startDrag(axis, clientX, clientY) {
            var b = bars[axis];
            var vertical = axis === 'y';
            var start = vertical ? clientY : clientX;
            var startScroll = vertical ? scroller.scrollTop : scroller.scrollLeft;
            var sh = vertical ? scroller.scrollHeight : scroller.scrollWidth;
            var ch = vertical ? scroller.clientHeight : scroller.clientWidth;
            var track = trackLen(axis);
            var th = Math.max(MIN_THUMB, (ch / sh) * track);
            var maxPos = track - th;
            b.bar.classList.add('is-dragging');
            document.body.style.userSelect = 'none';

            function move(v) {
                var d = v - start;
                var delta = maxPos > 0 ? (d / maxPos) * (sh - ch) : 0;
                if (vertical) scroller.scrollTop = startScroll + delta;
                else scroller.scrollLeft = startScroll + delta;
            }
            function onMouseMove(ev) { move(vertical ? ev.clientY : ev.clientX); }
            function onTouchMove(ev) { if (ev.touches[0]) { move(vertical ? ev.touches[0].clientY : ev.touches[0].clientX); ev.preventDefault(); } }
            function end() {
                b.bar.classList.remove('is-dragging');
                document.body.style.userSelect = '';
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', end);
                window.removeEventListener('touchmove', onTouchMove);
                window.removeEventListener('touchend', end);
            }
            window.addEventListener('mousemove', onMouseMove);
            window.addEventListener('mouseup', end);
            window.addEventListener('touchmove', onTouchMove, { passive: false });
            window.addEventListener('touchend', end);
        }

        schedule();
    }

    function attachWithin(root) {
        if (!root || root.nodeType !== 1) return;
        if (root.matches && root.matches(SELECTOR)) attach(root);
        root.querySelectorAll(SELECTOR).forEach(attach);
    }

    function gc() {
        attached = attached.filter(function (s) {
            if (s.isConnected) return true;
            var bars = s._cbar || {};
            Object.keys(bars).forEach(function (k) {
                var bar = bars[k].bar;
                if (bar.parentNode) bar.parentNode.removeChild(bar);
            });
            return false;
        });
    }

    function boot() {
        attachWithin(document.body);
        if (!window.MutationObserver) return;
        // Scrollers that arrive later (htmx swaps, Alpine x-if/x-for, dialogs).
        new MutationObserver(function (muts) {
            var removed = false;
            for (var i = 0; i < muts.length; i++) {
                var m = muts[i];
                if (m.removedNodes.length) removed = true;
                for (var j = 0; j < m.addedNodes.length; j++) attachWithin(m.addedNodes[j]);
            }
            if (removed) gc();
        }).observe(document.body, { childList: true, subtree: true });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else boot();
})();
