#!/usr/bin/env python3
"""
build_index.py — turn WordNet into the sense index the linter consumes.

    pip install nltk
    python build_index.py

No arguments needed. If you cannot pass command-line flags — a phone editor,
a "run" button, an embedded interpreter — edit the CONFIG block below instead;
a bare run uses exactly those values and writes its output next to this file.
Flags still work where a terminal is available:

    python build_index.py --max-words 6000 --purge-corpus

What it does, in order:
  1. Keeps only POLYSEMOUS words. A single-sense word can never be flagged,
     so it is dead weight — this is where ~85% of the size goes.
  2. MERGES near-identical senses. WordNet splits hairs no human would;
     senses in the same lexicographer file with Wu-Palmer > --merge collapse.
  3. Places each merged sense on a 0-1 axis by 1-D classical MDS over
     pairwise semantic distance. Spread across that axis IS the risk signal.
  4. Emits JSON for the app's Load button, plus a dict.js you can drop
     beside the HTML and pick up automatically via a script tag.

Overlay: --overlay overlay.json injects terms WordNet lacks or gets wrong
(Apple-the-company, legal terms of art). Overlay senses are pinned to the
low end of the axis and tagged "overlay". Same shape as the output:

    { "instrument": ["noun", [["a formal legal document",
                              "document|written record|artifact", 0.06,
                              "overlay"]]] }
"""

import argparse, atexit, json, math, os, shutil, sys, tempfile

sys.dont_write_bytecode = True          # no __pycache__ if this gets imported

# =====================================================================
# CONFIG — edit these values if you can't pass command-line flags.
# Running the script with no arguments at all uses exactly this block.
# Any flag you do pass overrides the matching value.
# =====================================================================
CONFIG = {
    "out":            "senses.json",   # the index, as JSON
    "js":             "dict.js",       # drop-in "window.D_WORDNET = {...}" for the app
    "overlay":        None,            # optional hand-written entries, e.g. "overlay.json"
    "out_dir":        None,            # force a folder, e.g. "/storage/emulated/0/Download"
    "clipboard":      True,            # also copy the index to the clipboard if possible

    "max_words":      50000,           # WordNet only has ~20-25k polysemous words total,
                                        # so this effectively means "all of them"
    "max_senses":     4,               # senses kept per word
    "max_senses_raw": 12,              # senses considered before merging
    "merge":          0.90,            # Wu-Palmer threshold for collapsing near-duplicates
    "min_spread":     0.35,            # drop words whose senses all sit close together
    "gloss_chars":    600,             # max gloss length; WordNet's longest definition is ~505
                                        # chars, so this keeps every gloss intact, no ellipsis
    "path_depth":     3,               # hypernym levels shown per sense
    "pos":            "nv",            # n=nouns, v=verbs, a=adjectives, r=adverbs

    "fetch_corpus":   True,            # download WordNet if it isn't already there
    "purge_corpus":   False,           # delete it again afterwards (only what this run got)
}

# Output goes next to this script, unless that turns out to be app-private
# storage the Files app can't see — then it goes somewhere findable instead.
HERE = os.path.dirname(os.path.abspath(__file__))

# Android keeps app files here; a file manager will never show them.
_PRIVATE_MARKERS = ("/data/user/", "/data/data/", "/android/data/", "/android/obb/")

# Checked in order. First writable one wins.
_VISIBLE_DIRS = (
    "/storage/emulated/0/Download",
    "/sdcard/Download",
    "/storage/emulated/0/Documents",
    "/storage/emulated/0",
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Documents"),
)


def pick_output_dir(preferred=None):
    """Where to write so the files can actually be found afterwards.

    Always prefers a known Downloads folder over the script's own location —
    a file manager may not show app-private storage, and even in an ordinary
    location, Downloads is where you're going to look for it anyway.
    """
    if preferred:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    for folder in _VISIBLE_DIRS:
        if os.path.isdir(folder) and os.access(folder, os.W_OK):
            return folder
    return HERE                          # nothing better available


OUT_DIR = None                           # set once config is parsed


def resolve(path):
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(OUT_DIR or HERE, path)


def to_clipboard(text):
    """Best effort. Saves hunting for the file at all on phone interpreters."""
    for module, func in (("clipboard", "set"), ("pyperclip", "copy")):
        try:
            mod = __import__(module)
            getattr(mod, func)(text)
            return module
        except Exception:
            continue
    return None


# --------------------------------------------------------------- cleanup
_TEMPS = []          # partial files still on disk
_PURGE = [False]     # set from config once parsed, read by the exit handler
_FETCHED = []        # corpora this run downloaded, for optional purge


def _sweep():
    """Runs on every exit path — normal, exception, or Ctrl-C."""
    for path in _TEMPS:
        try:
            os.unlink(path)
        except OSError:
            pass
    _TEMPS.clear()


atexit.register(_sweep)


def atomic_write(path, text):
    """Write via a temp file in the same directory, then rename into place.

    The output is either the previous file or the complete new one — never a
    half-written index. The temp file is tracked so it is removed even if the
    process dies before the rename.
    """
    folder = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".build-", suffix=".part")
    _TEMPS.append(tmp)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)               # atomic on POSIX and Windows
    _TEMPS.remove(tmp)


def ensure_corpus(fetch):
    """Confirm WordNet is readable. Records anything downloaded for --purge-corpus."""
    import nltk
    for name in ("wordnet", "omw-1.4"):
        try:
            _FETCHED.append(None)       # placeholder keeps indices aligned
            _FETCHED.pop()
            nltk.data.find(f"corpora/{name}")
        except LookupError:
            if not fetch:
                sys.exit(f"corpus '{name}' missing — rerun with --fetch-corpus, "
                         f"or: python -c \"import nltk; nltk.download('{name}')\"")
            print(f"downloading {name}\u2026", file=sys.stderr)
            if not nltk.download(name, quiet=True):
                sys.exit(f"download of '{name}' failed — check the network")
            try:
                _FETCHED.append(str(nltk.data.find(f"corpora/{name}")))
            except LookupError:
                pass
    wn.synsets("test")                  # force the lazy loader to prove itself


def purge_corpus():
    """Remove only what this run downloaded. A pre-existing corpus is left alone."""
    for path in _FETCHED:
        target = path[:-4] if path.endswith(".zip") else path
        for candidate in (target, target + ".zip"):
            try:
                if os.path.isdir(candidate):
                    shutil.rmtree(candidate)
                elif os.path.isfile(candidate):
                    os.unlink(candidate)
                else:
                    continue
                print(f"purged {candidate}", file=sys.stderr)
            except OSError as exc:
                print(f"could not purge {candidate}: {exc}", file=sys.stderr)
    _FETCHED.clear()


try:
    from nltk.corpus import wordnet as wn
except ImportError:
    sys.exit("nltk missing — pip install nltk")

POS_NAME = {"n": "noun", "v": "verb", "a": "adj", "s": "adj", "r": "adv"}
STOP = set("""be is are was were been being have has had do does did will would can could
shall should may might must the a an and or but if then than that this these those of to
in on at by for with from as it its there here what which who whom how when where why not
no nor so such very more most other some any each every all both few many much""".split())


# ---------------------------------------------------------------- geometry
def mds1(dist):
    """Classical MDS to one dimension. Returns positions normalised to 0-1.

    Pure stdlib. Double-centres the squared-distance matrix into a Gram matrix,
    then finds its dominant eigenvector by power iteration.

    The matrix is shifted by a Gershgorin bound first. Without that, power
    iteration converges on whichever eigenvalue is largest in magnitude — which
    for a Gram matrix can be a negative one, giving a meaningless axis. Adding
    the shift makes every eigenvalue non-negative and preserves the ordering,
    so the dominant eigenvector is the one MDS actually wants.
    """
    n = len(dist)
    if n < 2:
        return [0.5] * n

    # Identical senses carry no spread. Without this guard the shifted matrix
    # degenerates to the identity and power iteration returns the seed vector,
    # reporting maximum spread for a word whose senses are indistinguishable.
    if max(max(row) for row in dist) < 1e-9:
        return [0.5] * n

    sq = [[d * d for d in row] for row in dist]
    row_mean = [sum(r) / n for r in sq]              # symmetric, so col means match
    grand = sum(row_mean) / n
    gram = [[-0.5 * (sq[i][j] - row_mean[i] - row_mean[j] + grand) for j in range(n)]
            for i in range(n)]

    shift = max(sum(abs(v) for v in row) for row in gram) or 1.0
    for i in range(n):
        gram[i][i] += shift

    x = [math.sin(i + 1) for i in range(n)]          # deterministic, unlikely to be orthogonal
    for _ in range(300):
        y = [sum(gram[i][j] * x[j] for j in range(n)) for i in range(n)]
        norm = math.sqrt(sum(v * v for v in y))
        if norm < 1e-12:
            break
        y = [v / norm for v in y]
        if max(abs(a - b) for a, b in zip(y, x)) < 1e-11:
            x = y
            break
        x = y

    lo, hi = min(x), max(x)
    if hi - lo < 1e-9:
        return [0.5] * n
    return [(v - lo) / (hi - lo) for v in x]


def distance(a, b):
    """Semantic distance in 0-1. Wu-Palmer where defined, lexname split otherwise."""
    try:
        s = a.wup_similarity(b)
    except Exception:
        s = None
    if s is None:
        return 1.0 if a.lexname() != b.lexname() else 0.5
    return 1.0 - s


# ---------------------------------------------------------------- shaping
def hypernym_path(syn, depth=3):
    paths = syn.hypernym_paths()
    if not paths:
        return syn.lexname().split(".")[-1]
    chain = min(paths, key=len)[:-1]          # drop the synset itself
    names = [s.lemma_names()[0].replace("_", " ") for s in reversed(chain)]
    return "|".join(names[:depth]) or syn.lexname().split(".")[-1]


def gloss_of(syn, limit=64):
    g = syn.definition().split(";")[0].strip()
    return g[: limit - 1].rstrip() + "\u2026" if len(g) > limit else g


def freq_of(syn, word):
    """WordNet's own sense ranking — sense 1 is the most common."""
    for i, lem in enumerate(syn.lemmas(), 1):
        if lem.name().lower() == word:
            return lem.count(), i
    return 0, 99


def merge(synsets, threshold):
    """Collapse senses that no human would distinguish. Keeps the first of each cluster."""
    clusters = []
    for s in synsets:
        for c in clusters:
            head = c[0]
            if head.lexname() != s.lexname():
                continue
            try:
                sim = head.wup_similarity(s)
            except Exception:
                sim = None
            if sim is not None and sim >= threshold:
                c.append(s)
                break
        else:
            clusters.append([s])
    return [c[0] for c in clusters]


def build_entry(word, pos, args):
    syns = wn.synsets(word, pos=pos)
    if len(syns) < 2:
        return None

    syns = syns[: args.max_senses_raw]
    syns = merge(syns, args.merge)
    if len(syns) < 2:
        return None

    dist = [[distance(a, b) for b in syns] for a in syns]
    xs = mds1(dist)

    ranked = []
    for syn, x in zip(syns, xs):
        count, rank = freq_of(syn, word)
        ranked.append((rank, count, syn, x))

    # Anchor the axis: the most common sense sits at the low end, so the
    # eigenvector's arbitrary sign doesn't flip positions between runs.
    ranked.sort(key=lambda r: r[0])
    if ranked[0][3] > 0.5:
        ranked = [(r, c, s, 1.0 - x) for (r, c, s, x) in ranked]

    spread = max(r[3] for r in ranked) - min(r[3] for r in ranked)
    if spread < args.min_spread:
        return None

    senses = [
        [gloss_of(s, args.gloss_chars), hypernym_path(s, args.path_depth),
         round(x, 3), f"sense {rank}"]
        for rank, _, s, x in ranked[: args.max_senses]
    ]
    return [POS_NAME.get(pos, pos), senses], spread


def usefulness(word, spread, n_senses):
    """Rank words by how much a misreading would cost, not by sense count."""
    return spread * (0.72 + 0.07 * n_senses) * (1.0 + 0.15 * (len(word) <= 6))


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=CONFIG["out"])
    p.add_argument("--js", default=CONFIG["js"], help="drop-in JS (window.D_WORDNET = {...})")
    p.add_argument("--overlay", default=CONFIG["overlay"], help="JSON of hand-written entries to merge in")
    p.add_argument("--max-words", type=int, default=CONFIG["max_words"])
    p.add_argument("--max-senses", type=int, default=CONFIG["max_senses"], help="senses kept per word")
    p.add_argument("--max-senses-raw", type=int, default=CONFIG["max_senses_raw"],
                   help="senses considered before merging")
    p.add_argument("--merge", type=float, default=CONFIG["merge"],
                   help="Wu-Palmer threshold for collapsing")
    p.add_argument("--min-spread", type=float, default=CONFIG["min_spread"],
                   help="drop words whose senses cluster")
    p.add_argument("--gloss-chars", type=int, default=CONFIG["gloss_chars"])
    p.add_argument("--path-depth", type=int, default=CONFIG["path_depth"])
    p.add_argument("--pos", default=CONFIG["pos"], help="which POS to include: n, v, a, r")
    p.add_argument("--out-dir", default=CONFIG["out_dir"], help="folder to write into")
    p.add_argument("--clipboard", dest="clipboard", action="store_true", default=CONFIG["clipboard"])
    p.add_argument("--no-clipboard", dest="clipboard", action="store_false")
    p.add_argument("--fetch-corpus", dest="fetch_corpus", action="store_true",
                   default=CONFIG["fetch_corpus"], help="download WordNet if it is missing")
    p.add_argument("--no-fetch-corpus", dest="fetch_corpus", action="store_false")
    p.add_argument("--purge-corpus", dest="purge_corpus", action="store_true",
                   default=CONFIG["purge_corpus"],
                   help="delete the corpus afterwards — only what this run downloaded")
    p.add_argument("--no-purge-corpus", dest="purge_corpus", action="store_false")

    # parse_known_args, not parse_args: some phone editors inject their own
    # argv entries, and an unknown flag should not kill the build.
    args, extra = p.parse_known_args()
    if extra:
        print(f"ignoring unrecognised arguments: {' '.join(extra)}", file=sys.stderr)

    global OUT_DIR
    OUT_DIR = pick_output_dir(args.out_dir)
    args.out = resolve(args.out)
    args.js = resolve(args.js)
    args.overlay = resolve(args.overlay)

    _PURGE[0] = args.purge_corpus
    ensure_corpus(args.fetch_corpus)

    seen, scored = set(), []
    for pos in args.pos:
        for i, syn in enumerate(wn.all_synsets(pos)):
            for lem in syn.lemma_names():
                w = lem.lower()
                if "_" in w or len(w) < 3 or w in STOP or (w, pos) in seen:
                    continue
                seen.add((w, pos))
                built = build_entry(w, pos, args)
                if not built:
                    continue
                entry, spread = built
                scored.append((usefulness(w, spread, len(entry[1])), w, entry))
            if i % 2000 == 0:
                print(f"  {pos}: {i} synsets, {len(scored)} kept", file=sys.stderr)

    scored.sort(key=lambda r: -r[0])
    index = {}
    for _, w, entry in scored:
        if w in index:                      # first POS wins; noun before verb
            continue
        index[w] = entry
        if len(index) >= args.max_words:
            break

    if args.overlay:
        with open(args.overlay) as fh:
            over = json.load(fh)
        for w, entry in over.items():
            if w in index and entry[0] == index[w][0]:
                index[w][1] = entry[1] + index[w][1][: args.max_senses - len(entry[1])]
            else:
                index[w] = entry
        print(f"overlay: merged {len(over)} entries", file=sys.stderr)

    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    atomic_write(args.out, payload)
    if args.js:
        atomic_write(args.js, "window.D_WORDNET = " + payload + ";\n")

    avg = sum(len(e[1]) for e in index.values()) / max(len(index), 1)
    print(f"\n{len(index)} words · {avg:.1f} senses avg · "
          f"{len(payload)/1024:.0f} KB raw (~{len(payload)/1024/3.5:.0f} KB gzipped)",
          file=sys.stderr)
    print("\nwritten to:", file=sys.stderr)
    for path in (args.out, args.js):
        if path:
            size = os.path.getsize(path) / 1024
            print(f"  {path}  ({size:.0f} KB)", file=sys.stderr)

    if OUT_DIR != HERE:
        print(f"\n(written to your Downloads folder rather than next to the script,\n"
              f" so it shows up in a file manager: {OUT_DIR})", file=sys.stderr)

    if args.clipboard:
        via = to_clipboard(payload)
        if via:
            print(f"\ncopied to the clipboard via {via} — paste straight into the linter",
                  file=sys.stderr)
        else:
            print("\nno clipboard module available (pip install pyperclip to enable)",
                  file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted — no partial output written", file=sys.stderr)
        sys.exit(130)
    finally:
        _sweep()
        if _PURGE[0]:
            purge_corpus()
