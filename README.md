# grammatisch-hotkey-solution

Answer Grammatisch exercises with the `1`, `2` and `3` keys instead of the
mouse, and gloss conjugation infinitives with `Ctrl+T`.

Grammatisch's answer buttons are mouse-only. If you're drilling a few hundred
items, that's a few hundred pointer moves for input that's fundamentally
three-way. This script adds the missing keyboard layer from outside the app.

While Grammatisch is frontmost, `1`/`2`/`3` press the first, second and third
answer button. `Ctrl+T` looks up the on-screen prompt word (the infinitive on
conjugation screens, where the word is not selectable so Grammatisch's own
Translation button has nothing to grab) and shows an English gloss. Every
other key, and every key in every other app, passes through untouched.

> **Not affiliated with or endorsed by the makers of Grammatisch.** This is an
> external helper that drives the app through the standard macOS accessibility
> API — the same interface screen readers use. It doesn't modify, patch or
> redistribute any part of the app.

---

## Before you install anything

This script needs macOS **Accessibility** permission, which is a real
privilege: it allows intercepting keyboard input system-wide. You should not
grant that to a script from a stranger on the internet without looking at it
first.

So: [read `grammatisch_keys.py`](grammatisch_keys.py). It's one file with
comments, no obfuscation. Specifically, you can verify that it:

- only acts when the frontmost app's bundle ID is `CI.GrammarApp`
- only swallows bare `1`/`2`/`3` and `Ctrl+T`; every other event passes through
- writes nothing to disk
- does **no network** for answering; `Ctrl+T` fetches a gloss for that one word
  from Wiktionary, then Google Translate only if Wiktionary has no entry

If any of that doesn't hold when you read it, please open an issue.

---

## Requirements

- macOS
- Python 3
- `pyobjc-framework-Quartz` and `pyobjc-framework-ApplicationServices`

## Install

Check whether you already have the pyobjc frameworks:

```sh
python3 -c "import Quartz, ApplicationServices; print('ok')"
```

If that fails:

```sh
python3 -m pip install --user \
    pyobjc-framework-Quartz pyobjc-framework-ApplicationServices
```

## Grant Accessibility permission

macOS attaches this permission to the program that **launches** the script, not
to Python itself. So the grant goes to your terminal:

    System Settings → Privacy & Security → Accessibility → enable Terminal

Then **fully quit and reopen that terminal** (⌘Q, not just closing the window).
The grant is only read at launch, so a still-running terminal keeps the old
answer and the script will report no permission even though the toggle looks on.

If you run the script from an IDE's integrated terminal — VS Code, Cursor,
PyCharm — grant that application instead, since it's the one that launched
Python. This is the single most common reason the script reports no permission.

## Check

With Grammatisch open **on a question screen**:

```sh
python3 grammatisch_keys.py --check
```

```
ok Grammatisch is running (pid 51234).
ok Accessibility permission granted.
ok Found 3 answer button(s):
     1. der  @(712,528)
     2. die  @(712,579)
     3. das  @(712,630)
```

On a conjugation or text screen you'll see the prompt word instead, which is
correct. On the exercise picker you'll see `No answer buttons found` — see
*Inert outside questions* below.

## Run

```sh
python3 grammatisch_keys.py
```

```
Listening. 1 / 2 / 3 answer Grammatisch while it's frontmost.
Ctrl+T glosses the on-screen word. Esc or click the gloss to close.
Ctrl-C to quit.
1 -> der
3 -> das
t  ergeben
   to yield, produce; to make sense; to surrender  [wiktionary]
```

Leave it running, switch to Grammatisch, and answer with the number keys. On a
conjugation screen, `Ctrl+T` (not bare `t`, so `ergibt` still types) shows a
one-line caption under the infinitive. Esc, a click on the caption, or Ctrl+T
again closes it.

---

## How it works

- **`CGEventTap`** on `kCGSessionEventTap` watches for keydowns (and left clicks
  while a gloss is showing). On a match the handler returns `None`, which
  swallows the event so the digit isn't also typed into the app. Everything
  else is returned untouched.
- **App scoping** is a `frontmostApplication()` check against bundle ID
  `CI.GrammarApp` on every keypress. If Grammatisch isn't frontmost the key
  passes through, so `1` still types `1` everywhere else.
- **Window scoping**: Grammatisch keeps the exercise picker open behind the
  quiz window, and the picker has its own answer-sized button. Only the focused
  window is searched.
- **Button lookup is structural, not label-based.** It collects button-like
  accessibility elements, drops anything too small or too large to be an answer
  (toolbar icons, window controls), then keeps the largest vertical cluster
  sharing an x-position and width. This is why it survives exercise types other
  than der/die/das, where the same three slots hold different words.
- **Inert outside questions**: with no clean vertical stack of at least two
  buttons, it returns nothing and the keypress falls through to the app. Without
  this, `1` on the exercise picker would press *Learn it*.
- **Pressing** tries `AXPress` first, falling back to a synthetic click on the
  button's centre if the app doesn't implement the action.
- **Modifiers**: cmd, ctrl or alt held → pass through, so `⌘1` and friends keep
  working. **Exception:** `Ctrl+T` looks up the prompt word.
- **Gloss lookup**: the conjugation infinitive is not selectable, so the in-app
  Translation button cannot grab it. Ctrl+T reads the prompt from the
  accessibility tree (`Conjugate the verb, ergeben`), asks Wiktionary for
  English senses, and falls back to Google Translate only if Wiktionary 404s.
  The lookup runs on a background thread so the event tap is not blocked.
- **Keycodes** 18/19/20 are physical key positions, identical on US QWERTY and
  German QWERTZ, so your keyboard layout doesn't matter. `T` is keycode 17.

## Troubleshooting

**"No Accessibility permission"** with the toggle switched on — you almost
certainly didn't fully quit the terminal after granting, or you granted
Terminal while running the script from an IDE. See above.

**"No answer buttons found"** on a question screen — dump the accessibility
tree and open an issue with the output:

```sh
python3 grammatisch_keys.py --probe
```

Likely means the app's layout changed in an update and the size heuristics in
`answer_buttons()` need adjusting.

**Keys occasionally dropped** — macOS disables an event tap that blocks for too
long. The script re-arms it automatically, but the keypress that triggered the
timeout is lost. Caching the button list per question would fix it; it hasn't
been needed in practice.

## Optional: run at login

`local.grammatisch-hotkey-solution.plist` is a LaunchAgent template. Edit the two paths
inside it, then:

```sh
cp local.grammatisch-hotkey-solution.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/local.grammatisch-hotkey-solution.plist
```

Caveat: a launchd process isn't a child of your terminal, so it does **not**
inherit that Accessibility grant. You'll be prompted again, and the grant
attaches to the Python binary itself — which breaks when you upgrade Python.
Running it by hand is less fragile.

## Ideally, this shouldn't exist

The right fix is number-key shortcuts — and a working translation control on
conjugation screens — in the app itself. A feature request has been sent to the
developers. If they add it, this repo becomes unnecessary, which would be the
good outcome.

## License

MIT — see [LICENSE](LICENSE).
