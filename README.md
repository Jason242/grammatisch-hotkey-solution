# grammatisch-hotkey-solution

Answer Grammatisch exercises with the `1`, `2` and `3` keys instead of the
mouse.

Grammatisch's answer buttons are mouse-only. If you're drilling a few hundred
items, that's a few hundred pointer moves for input that's fundamentally
three-way. This script adds the missing keyboard layer from outside the app.

While Grammatisch is frontmost, `1`/`2`/`3` press the first, second and third
answer button. Every other key, and every key in every other app, passes
through untouched.

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

So: [read `grammatisch_keys.py`](grammatisch_keys.py). It's ~450 lines with
comments, single file, no obfuscation. Specifically, you can verify that it:

- has **no network code whatsoever** — nothing is sent anywhere, ever
- only acts when the frontmost app's bundle ID is `CI.GrammarApp`
- only reacts to bare `1`/`2`/`3` keydowns, and passes every other event
  through unmodified
- writes nothing to disk

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

On the exercise picker instead of a question you'll see
`No answer buttons found`. That's correct — see *Inert outside questions* below.

## Run

```sh
python3 grammatisch_keys.py
```

```
Listening. 1 / 2 / 3 answer Grammatisch while it's frontmost.
Ctrl-C to quit.
1 -> der
3 -> das
```

Leave it running, switch to Grammatisch, and answer with the number keys.

---

## How it works

- **`CGEventTap`** on `kCGSessionEventTap` watches for keydowns. On a match the
  handler returns `None`, which swallows the event so the digit isn't also
  typed into the app. Everything else is returned untouched.
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
  working.
- **Keycodes** 18/19/20 are physical key positions, identical on US QWERTY and
  German QWERTZ, so your keyboard layout doesn't matter.

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

The right fix is number-key shortcuts in the app itself. A feature request has
been sent to the developers. If they add it, this repo becomes unnecessary,
which would be the good outcome.

## License

MIT — see [LICENSE](LICENSE).
