# NQX Nightwatch Mini App — local prototype

This is a local-only visual prototype with a dark underground "private channel" treatment, an architectural Three.js scene, animated snake pulse, and a blackletter display voice. The 3D canvas is decorative and never receives pointer events. It does not call `order.py`, CrossTrade, or any live API.

## Run

From the project directory:

```powershell
npm install
npm run dev -- --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> in a browser. The Google Fonts link is only for the prototype; `UnifrakturCook` / `Pirata One` are used for the occult display layer and `IBM Plex Mono` keeps prices and controls readable. Local fallback stacks are included.

For a static host, run `npm run build` and upload the contents of `dist/`. The Vite build is the only
supported deployment artifact: `three` / `gsap` / `postprocessing` are bundled into the lazy
`three-renderer` chunk, and the build refuses to load any script from a third-party CDN. `npm run build`
also regenerates `public/_headers`, which carries the Content-Security-Policy and `Referrer-Policy`
that keep the launch token from leaking out of the page — deploy `dist/` to Cloudflare Pages so those
headers are actually served.

## Telegram bridge boundary

When opened inside Telegram, `app.js` detects `Telegram.WebApp`, marks the bridge as ready, and sends only a demo `scenario_preview` payload through `sendData`. Production wiring must validate Telegram `initData` on the server, re-check the allowed chat ID, and route to the existing dry-run path before any confirmation can be shown. No secret belongs in this folder.
