/* main.js — bundle entry for the control page (static/js/atem_control.js).
 *
 * The per-feature modules under atem_control/js/ are composed
 * here; build/js/build.sh bundles this file into the served
 * static/js/atem_control.js (see that script for the dev loop).
 */

import './audio_widgets.js';
import './chroma_sample.js';
import './runtime.js';
import './store.js';

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    ATEMControl.init();
});