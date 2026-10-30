# Vendored library

`msal-browser.min.js` is Microsoft's MSAL.js for browsers, **@azure/msal-browser 4.30.0** (MIT licence, see
`msal-browser.LICENSE.txt`), copied unmodified from the npm package's `lib/msal-browser.min.js`.
SHA-256: `2b580165064a6a4ec041ebd44832299172688a23a42c3b81d65314799dba04eb`

It is served from this app's own origin (the page's security policy only allows scripts from `'self'`), and `app.js` loads
it only when `AUTH_MODE` is `entra` or `hybrid`. To update: `npm install @azure/msal-browser@<version>`, copy the file,
update the version in `app.js` and the hash above, and re-test sign-in.
