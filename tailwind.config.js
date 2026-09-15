/**
 * Build-time Tailwind config. Scans every template and first-party JS file
 * for class names and emits only what's used (theme tokens live in
 * atem_control/static/brand/brand.css, not here). Produces
 * atem_control/static/vendor/webatem.css in the Docker build — replacing
 * the Tailwind Play CDN and the daisyUI CDN stylesheet.
 */
module.exports = {
  content: [
    './webatem/templates/**/*.html',
    './atem_control/templates/**/*.html',
    './atem_control/static/js/**/*.js',
  ],
  theme: { extend: {} },
  plugins: [require('daisyui')],
  daisyui: {
    // No built-in themes: the app's ONE theme (dark) is defined in
    // atem_control/static/brand/brand.css in DaisyUI 4's variable syntax and
    // loaded after this sheet. base.html sets data-theme="dark".
    themes: false,
    logs: false,
  },
};
