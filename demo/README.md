# CommVoice — functional demo (browser + Gemini)

A **self-contained, functional** demo of CommVoice styled as an Australian Government service.
It holds a real conversation grounded in a sample official document, running Google Gemini
**directly in the browser** — no backend server required, so it can be published on GitHub Pages.

Files:
- `index.html` — the app (HTML, CSS, JS in one file; Australian-Government look)
- `doc-data.js` — the sample document text (City of Sydney draft planning agreement,
  155 Mitchell Road Erskineville), embedded per page so answers can cite page numbers

## How it works

1. Open the page and **paste a free Google Gemini API key** (from
   [aistudio.google.com/apikey](https://aistudio.google.com/apikey)).
   The key is stored **only in your browser** (`localStorage`) — it is never committed to the
   repo, never sent to this website, only to Google's Gemini API.
2. Click **Open the sample & explain it** → Gemini writes a plain-words overview.
3. **(Optional) natural voice** — expand *"Optional: natural voice with ElevenLabs"* and paste an
   ElevenLabs API key (from [elevenlabs.io](https://elevenlabs.io/app/settings/api-keys)). The demo
   then fetches the voices on that account, lets you **choose one from a dropdown** and preview it,
   and reads answers aloud in that natural voice. Without a key, answers use the browser's built-in
   voice. The ElevenLabs key is also stored only in your browser.
4. **Ask anything in your own words**, by typing or voice. The assistant answers **only from the
   document** and shows the exact quote + page. Ask something not covered (e.g. council rates)
   and it declines honestly instead of guessing.

## The guardrail

The system prompt forces the model to answer only from the supplied document text, to attach a
`SOURCE: page N | "quote"` line, and to reply `SOURCE: none` when the document doesn't cover the
question — which the UI renders as an honest "Not in the document" refusal. (The full production
app in the repo root enforces this in code with a four-gate verifier; this browser demo enforces
the spirit of it via the prompt.)

## Run locally

Because it loads `doc-data.js`, open it through a tiny local server (not file://):

```
cd demo
python3 -m http.server 8000
# then open http://127.0.0.1:8000/
```

## Publish (GitHub Pages)

Settings → Pages → Deploy from a branch → `main` / root. The demo will be at:
`https://<owner>.github.io/CommVoice/demo/`

## Notes for the hackathon team

- Free Gemini tier has low rate limits and Google may use submitted data — fine for this public
  sample document; don't upload anything confidential.
- Voice input uses the browser's speech recognition (best in Chrome/Edge). "Read aloud" uses the
  browser's speech synthesis.
- Everyone who opens the link uses **their own** key, so there's no shared cost or exposed secret.
