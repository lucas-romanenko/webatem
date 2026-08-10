/**
 * Build-time Tailwind config. Scans every template and first-party JS file
 * for class names and emits only what's used, plus the two daisyUI themes
 * the app's theme toggle switches between (dark / corporate). Produces
 * atem_control/static/vendor/webatem.css in the Docker build — replacing
 * the Tailwind Play CDN and the daisyUI CDN stylesheet.
 */
module.exports = {
  content: [
    './templates/**/*.html',
    './atem_control/templates/**/*.html',
    './atem_control/static/js/**/*.js',
  ],
  theme: { extend: {} },
  plugins: [require('daisyui')],
  daisyui: {
    themes: ['dark', 'corporate'],
    logs: false,
  },
};
