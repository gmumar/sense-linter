# Sense

A single-page prompt ambiguity linter. It marks the words in your prompt most
likely to be read a different way than you meant, using a WordNet-derived
sense index — no model, no network calls, runs entirely offline in the
browser.

## Use it

Open `sense-linter.html` directly, or serve the folder and visit it — the
full dictionary (`dict.js`) loads automatically in the background.

Live: https://gmumar.github.io/sense-linter/

## Files

- `sense-linter.html` — the app (markup, styles, logic)
- `dict.js` / `senses.json` — the generated WordNet sense index
- `build_index.py` — regenerates the index from WordNet (`pip install nltk`)

## License

MIT — see [LICENSE](LICENSE).
