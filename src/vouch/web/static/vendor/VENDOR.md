# vendored frontend assets

- `vue.esm-browser.prod.js` — Vue 3.4.38, full build (includes the template
  compiler so `template:` strings compile at runtime). Source:
  https://unpkg.com/vue@3.4.38/dist/vue.esm-browser.prod.js
  sha256: c6cae9e178887adc3006f7ed7c9adf0a582eddeb918e8b8e7a50f0e15c025a1c

  buildless on purpose: no npm/bundler. bump = re-download a pinned version,
  update this file's version + sha, and re-run the manual smoke checklist.
