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
    // 'dark' is OUR ASC-gray palette (overrides the daisyUI builtin of the
    // same name, so the existing theme toggle and data-theme wiring stay
    // untouched): neutral grays like ATEM Software Control, no blue tint.
    // Tally colors ride utility classes (red-600 / green-600 / yellow) and
    // are unaffected here.
    themes: [
      {
        dark: {
          'color-scheme': 'dark',
          primary: '#E8B23D',
          'primary-content': '#201803',
          secondary: '#4A4E54',
          'secondary-content': '#E2E4E7',
          accent: '#E8B23D',
          'accent-content': '#201803',
          neutral: '#3A3D42',
          'neutral-content': '#E2E4E7',
          'base-100': '#2E3033',
          'base-200': '#26282B',
          'base-300': '#46494E',
          'base-content': '#DDDFE2',
          info: '#5FA4D8',
          success: '#3FA653',
          warning: '#E8B23D',
          error: '#D2222D',
        },
      },
      'corporate',
    ],
    logs: false,
  },
};
