# Marketing_landing-pages

A single-page, fully client-side marketing tool ("РТС-тендер | AI Промпт-конструктор") that
generates AI prompts from form input. All markup, styles, and vanilla JS live inline in
`index.html` (`html_service` is an identical copy of it).

## Cursor Cloud specific instructions

- This is a static site with **no dependencies, no build step, no tests, and no backend**.
  There is no package manager file, so no install command is needed.
- To run it in development, serve the repo root with any static file server and open
  `index.html`, e.g. `python3 -m http.server 8000` then visit `http://localhost:8000/index.html`.
  Opening the file via `file://` also works since there are no network requests.
- All logic is inline `<script>` in `index.html`; there is no hot reload — refresh the browser
  after edits.
- `html_service` and `index.html` are byte-identical duplicates. If you change one, mirror the
  change in the other to keep them in sync.
