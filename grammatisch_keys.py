#!/usr/bin/env python3
"""
grammatisch_keys.py -- answer Grammatisch with the 1 / 2 / 3 keys.

Grammatisch ships mouse-only answer buttons. This adds the missing keyboard
layer: while Grammatisch is frontmost, pressing 1, 2 or 3 presses the first,
second or third answer button. Every other key, and every key in every other
app, passes through untouched.

Ctrl+T looks up the on-screen prompt word (e.g. a conjugation infinitive)
and shows an English gloss. That path talks to Wiktionary, then Google
Translate only if Wiktionary has no entry. 1/2/3 never go to the network.

No GUI app is installed. Two moving parts plus an optional lookup:

  * a CGEventTap that watches for bare 1/2/3 and Ctrl+T
  * the macOS Accessibility API, used to locate buttons and the prompt
  * an on-demand gloss lookup, only when you press Ctrl+T

Usage:
    python3 grammatisch_keys.py            # run the listener
    python3 grammatisch_keys.py --probe    # dump Grammatisch's AX tree
    python3 grammatisch_keys.py --check    # verify permissions and exit
    python3 grammatisch_keys.py --translate  # look up the current prompt and exit

Requires: pyobjc-framework-Quartz, pyobjc-framework-ApplicationServices
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# imports
# --------------------------------------------------------------------------

try:
    import Quartz
    from AppKit import (
        NSApplication,
        NSBackingStoreBuffered,
        NSColor,
        NSFloatingWindowLevel,
        NSFont,
        NSLineBreakByTruncatingTail,
        NSMakePoint,
        NSMakeRect,
        NSPanel,
        NSPointInRect,
        NSScreen,
        NSTextAlignmentCenter,
        NSTextField,
        NSWorkspace,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from ApplicationServices import (
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        AXUIElementPerformAction,
        AXValueGetValue,
        kAXValueCGPointType,
        kAXValueCGSizeType,
    )
except ImportError:
    sys.exit(
        textwrap.dedent(
            """\
            Missing pyobjc. Install it with:

                python3 -m pip install --user \\
                    pyobjc-framework-Quartz pyobjc-framework-ApplicationServices
            """
        )
    )

try:
    from ApplicationServices import (
        AXIsProcessTrustedWithOptions,
        kAXTrustedCheckOptionPrompt,
    )
except ImportError:  # older / differently-packaged pyobjc
    AXIsProcessTrustedWithOptions = None
    kAXTrustedCheckOptionPrompt = None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

BUNDLE_ID = "CI.GrammarApp"

# Physical keycodes for the number row. Layout-independent across US QWERTY
# and German QWERTZ, which share the top-row digit positions.
KEYCODE_TO_SLOT = {18: 0, 19: 1, 20: 2}  # 1 -> first button, etc.
KEYCODE_T = 17  # physical T, same on QWERTY and QWERTZ
KEYCODE_ESCAPE = 53

# Caption under the infinitive. Sized to sit in the ~37px gap above Präsens
# so it does not cover the prompt word.
HUD_HEIGHT = 32
HUD_WIDTH = 560

# Bare 1/2/3 only. If any of these modifiers are held, pass those keys through
# so app and system shortcuts (cmd+1, ctrl+2, ...) keep working. Ctrl+T is
# handled separately -- bare T must still type into conjugation blanks.
BLOCKING_MODIFIERS = (
    Quartz.kCGEventFlagMaskCommand
    | Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
)

HTTP_TIMEOUT_SECONDS = 4
USER_AGENT = "grammatisch-hotkey-solution/1.0 (personal helper; +https://github.com/Jason242/grammatisch-hotkey-solution)"

# English instruction + German lemma, as Grammatisch exposes it to AX.
# The word itself is not selectable on conjugation screens, so the in-app
# Translation button has nothing to grab -- we parse this string instead.
LEMMA_PATTERNS = (
    re.compile(r"conjugate the verb[,:\s]+([A-Za-zÄÖÜäöüß\-]+(?:\s+sich)?)", re.I),
    re.compile(r"choose the article[,:\s]+([A-Za-zÄÖÜäöüß\-]+)", re.I),
)

CHROME_LABELS = {
    "grammatisch",
    "translation",
    "präsens",
    "präteritum",
    "perfekt",
    "plusquamperfekt",
    "futur",
    "futur i",
    "futur ii",
    "imperativ",
    "konjunktiv",
    "konjunktiv i",
    "konjunktiv ii",
    "ich",
    "du",
    "er/sie/es",
    "wir",
    "ihr",
    "sie/sie",
}

_GLOSS_CACHE: dict[str, tuple[str, str]] = {}
_HUD = None
_RUNLOOP = None

# Elements that behave like answer buttons in SwiftUI / AppKit / Electron apps.
BUTTONISH_ROLES = {"AXButton", "AXRadioButton", "AXCheckBox", "AXLink", "AXCell"}

MAX_DEPTH = 40


# --------------------------------------------------------------------------
# accessibility helpers
# --------------------------------------------------------------------------


def ax_get(element, attribute):
    """Read one AX attribute, or None if unreadable."""
    try:
        err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    return value if err == 0 else None


def ax_point(element):
    raw = ax_get(element, "AXPosition")
    if raw is None:
        return None
    try:
        ok, point = AXValueGetValue(raw, kAXValueCGPointType, None)
    except Exception:
        return None
    return (point.x, point.y) if ok else None


def ax_size(element):
    raw = ax_get(element, "AXSize")
    if raw is None:
        return None
    try:
        ok, size = AXValueGetValue(raw, kAXValueCGSizeType, None)
    except Exception:
        return None
    return (size.width, size.height) if ok else None


def ax_label(element) -> str:
    """Best available human-readable label for an element."""
    for attribute in ("AXTitle", "AXValue", "AXDescription", "AXLabel", "AXHelp"):
        value = ax_get(element, attribute)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def walk(element, depth: int = 0):
    """Depth-first walk of the AX tree."""
    yield element, depth
    if depth >= MAX_DEPTH:
        return
    for child in ax_get(element, "AXChildren") or []:
        yield from walk(child, depth + 1)


def running_app():
    """The NSRunningApplication for Grammatisch, or None."""
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.bundleIdentifier() == BUNDLE_ID:
            return app
    return None


def frontmost_is_target() -> bool:
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    return bool(front and front.bundleIdentifier() == BUNDLE_ID)


def app_element():
    """AXUIElement for Grammatisch, or None if it isn't running."""
    app = running_app()
    if app is None:
        return None
    return AXUIElementCreateApplication(app.processIdentifier())


def target_window(app):
    """
    The window the user is actually looking at.

    Grammatisch keeps the exercise picker open behind the quiz window, and that
    picker has its own button of answer-like proportions ("Learn it", 144x40).
    Walking the whole application picks up both windows' buttons, so scope to
    one window first.
    """
    if app is None:
        return None
    for attribute in ("AXFocusedWindow", "AXMainWindow"):
        window = ax_get(app, attribute)
        if window is not None:
            return window
    windows = ax_get(app, "AXWindows") or []
    return windows[0] if windows else app


def answer_buttons(root):
    """
    Grammatisch's answer buttons, ordered top to bottom.

    Identified structurally rather than by label, so this keeps working across
    exercise types -- article drills show der/die/das, but other drills show
    different words in the same three slots.

    Observed on the article drill: answers are 132x39, the Translation toolbar
    button is 38x34, window controls are 12x14. The width floor separates them.
    """
    found = []
    for element, _ in walk(root):
        if ax_get(element, "AXRole") not in BUTTONISH_ROLES:
            continue
        point = ax_point(element)
        size = ax_size(element)
        if point is None or size is None:
            continue
        width, height = size
        # Skip window chrome: traffic lights, toolbar icons, and anything
        # too small or too wide to be a stacked answer button.
        if width < 60 or height < 20 or height > 140:
            continue
        found.append(
            {
                "element": element,
                "label": ax_label(element),
                "x": point[0],
                "y": point[1],
                "w": width,
                "h": height,
            }
        )

    if not found:
        return []

    # Answer buttons form a vertical stack: same width, similar x. Keep the
    # largest such cluster and drop stray toolbar buttons.
    found.sort(key=lambda b: b["y"])
    best: list[dict] = []
    for anchor in found:
        cluster = [
            b
            for b in found
            if abs(b["x"] - anchor["x"]) < 25 and abs(b["w"] - anchor["w"]) < 25
        ]
        if len(cluster) > len(best):
            best = cluster

    # No clean vertical stack means this isn't a multiple-choice screen -- the
    # exercise picker, a results screen, a dialog. Returning `found` there would
    # make 1/2/3 press whatever button happened to survive the size filter, so
    # return nothing and let the keys fall through to the app untouched.
    return best if len(best) >= 2 else []


def press(button) -> bool:
    """Press a button via AX, falling back to a synthetic click on its centre."""
    try:
        if AXUIElementPerformAction(button["element"], "AXPress") == 0:
            return True
    except Exception:
        pass

    centre = Quartz.CGPointMake(button["x"] + button["w"] / 2, button["y"] + button["h"] / 2)
    try:
        for event_type, mouse in (
            (Quartz.kCGEventLeftMouseDown, Quartz.kCGMouseButtonLeft),
            (Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
        ):
            event = Quartz.CGEventCreateMouseEvent(None, event_type, centre, mouse)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# prompt lemma + English gloss
# --------------------------------------------------------------------------


def _lemma_from_label(label: str) -> str | None:
    text = label.strip()
    if not text:
        return None
    for pattern in LEMMA_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    lowered = text.lower()
    if lowered in CHROME_LABELS:
        return None
    if "," in text:
        right = text.rsplit(",", 1)[1].strip()
        token = right.split()[0] if right else ""
        if token and token.lower() not in CHROME_LABELS and re.fullmatch(
            r"[A-Za-zÄÖÜäöüß\-]+", token
        ):
            return token
    if re.fullmatch(r"[A-Za-zÄÖÜäöüß\-]+", text) and lowered not in CHROME_LABELS:
        return text
    return None


def prompt_anchor(root) -> dict | None:
    """Prompt word plus AX frames used to park the gloss under the infinitive."""
    window_pt = ax_point(root)
    window_sz = ax_size(root)
    prompt = None
    speaker = None
    heading = None
    word = None

    for element, _ in walk(root):
        role = ax_get(element, "AXRole")
        label = ax_label(element)
        point = ax_point(element)
        size = ax_size(element)
        if point is None or size is None:
            continue
        frame = (point[0], point[1], size[0], size[1])
        if role == "AXButton" and label == "speaker.2":
            speaker = frame
            continue
        if role == "AXHeading" and label.lower() in CHROME_LABELS:
            if heading is None or point[1] < heading[1]:
                heading = frame
            continue
        if role not in {"AXStaticText", "AXHeading"}:
            continue
        found = _lemma_from_label(label)
        if found and size[1] >= 60:
            word = found
            prompt = frame

    if word is None:
        return None
    window = None
    if window_pt and window_sz:
        window = (window_pt[0], window_pt[1], window_sz[0], window_sz[1])
    return {
        "word": word,
        "window": window,
        "prompt": prompt,
        "speaker": speaker,
        "heading": heading,
    }


def prompt_lemma(root) -> str | None:
    """
    The German word the current screen is drilling.

    Conjugation prompts are one AXStaticText like 'Conjugate the verb, ergeben'.
    The infinitive is not a selectable AX text range, which is why Grammatisch's
    own Translation button does nothing here.
    """
    anchor = prompt_anchor(root)
    return None if anchor is None else anchor["word"]


def _strip_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _http_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _gloss_wiktionary(word: str) -> str | None:
    """English senses for a German headword. Better for drills than machine MT."""
    url = "https://en.wiktionary.org/api/rest_v1/page/definition/" + urllib.parse.quote(
        word
    )
    try:
        data = _http_json(url)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    entries = data.get("de") or data.get("en") or []
    senses: list[str] = []
    for entry in entries:
        for definition in entry.get("definitions") or []:
            text = _strip_markup(str(definition.get("definition") or ""))
            if text:
                senses.append(text)
            if len(senses) >= 3:
                break
        if len(senses) >= 3:
            break
    return "; ".join(senses) if senses else None


def _gloss_google(word: str) -> str | None:
    url = (
        "https://translate.googleapis.com/translate_a/single"
        "?client=gtx&sl=de&tl=en&dt=t&q="
        + urllib.parse.quote(word)
    )
    data = _http_json(url)
    chunks = data[0] if data else None
    if not chunks:
        return None
    parts = [chunk[0] for chunk in chunks if chunk and chunk[0]]
    text = " ".join(parts).strip()
    return text or None


def english_gloss(word: str) -> tuple[str, str]:
    cached = _GLOSS_CACHE.get(word)
    if cached is not None:
        return cached
    gloss = _gloss_wiktionary(word)
    source = "wiktionary"
    if not gloss:
        gloss = _gloss_google(word)
        source = "google"
    if not gloss:
        raise RuntimeError("no gloss from Wiktionary or Google")
    rendered = (gloss, source)
    _GLOSS_CACHE[word] = rendered
    return rendered


def _primary_screen_height() -> float:
    for screen in NSScreen.screens():
        origin = screen.frame().origin
        if origin.x == 0 and origin.y == 0:
            return float(screen.frame().size.height)
    return float(NSScreen.mainScreen().frame().size.height)


def ax_to_cocoa_rect(ax_x, ax_y, width, height):
    """AX is top-left origin; Cocoa windows are bottom-left."""
    screen_h = _primary_screen_height()
    return (ax_x, screen_h - ax_y - height, width, height)


def hud_ax_rect(anchor: dict) -> tuple[float, float, float, float]:
    window = anchor.get("window")
    prompt = anchor.get("prompt")
    speaker = anchor.get("speaker")
    heading = anchor.get("heading")
    width = HUD_WIDTH
    height = HUD_HEIGHT
    if window:
        width = min(HUD_WIDTH, max(320, window[2] - 80))
        ax_x = window[0] + (window[2] - width) / 2
    elif prompt:
        ax_x = prompt[0] + (prompt[2] - width) / 2
    else:
        ax_x = 0

    if speaker:
        ax_y = speaker[1] + speaker[3] + 4
    elif prompt:
        ax_y = prompt[1] + prompt[3] - height
    else:
        ax_y = 200

    if heading and ax_y + height > heading[1] - 2:
        ax_y = heading[1] - height - 4
    return (ax_x, ax_y, width, height)


class GlossHud:
    """Borderless caption parked under the infinitive, matching Grammatisch's page."""

    def __init__(self):
        NSApplication.sharedApplication()
        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, HUD_WIDTH, HUD_HEIGHT),
            style,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setOpaque_(True)
        self.panel.setHasShadow_(False)
        self.panel.setMovable_(False)
        self.panel.setBackgroundColor_(NSColor.whiteColor())
        self.panel.setReleasedWhenClosed_(False)
        self.field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(12, 0, HUD_WIDTH - 24, HUD_HEIGHT)
        )
        self.field.setEditable_(False)
        self.field.setBezeled_(False)
        self.field.setBordered_(False)
        self.field.setDrawsBackground_(False)
        self.field.setSelectable_(False)
        self.field.setAlignment_(NSTextAlignmentCenter)
        self.field.setLineBreakMode_(NSLineBreakByTruncatingTail)
        self.field.setFont_(NSFont.systemFontOfSize_(16))
        self.field.setTextColor_(NSColor.secondaryLabelColor())
        self.panel.contentView().addSubview_(self.field)
        self.word = None

    def visible(self) -> bool:
        return bool(self.panel.isVisible())

    def contains_cocoa_point(self, x, y) -> bool:
        if not self.visible():
            return False
        return bool(NSPointInRect(NSMakePoint(x, y), self.panel.frame()))

    def hide(self):
        self.panel.orderOut_(None)
        self.word = None

    def show(self, anchor: dict, gloss: str):
        ax_x, ax_y, width, height = hud_ax_rect(anchor)
        cocoa_x, cocoa_y, _, _ = ax_to_cocoa_rect(ax_x, ax_y, width, height)
        self.panel.setFrame_display_(NSMakeRect(cocoa_x, cocoa_y, width, height), True)
        self.field.setFrame_(NSMakeRect(12, 0, width - 24, height))
        self.field.setStringValue_(gloss)
        self.word = anchor["word"]
        self.panel.orderFrontRegardless()


def _ensure_hud() -> GlossHud:
    global _HUD
    if _HUD is None:
        _HUD = GlossHud()
    return _HUD


def hud_is_visible() -> bool:
    return _HUD is not None and _HUD.visible()


def hide_hud() -> None:
    if _HUD is None:
        return

    def dismiss():
        _HUD.hide()

    if _RUNLOOP is None:
        dismiss()
        return
    Quartz.CFRunLoopPerformBlock(_RUNLOOP, Quartz.kCFRunLoopCommonModes, dismiss)
    Quartz.CFRunLoopWakeUp(_RUNLOOP)


def show_gloss(anchor: dict, gloss: str, source: str) -> None:
    print(f"t  {anchor['word']}\n   {gloss}  [{source}]", flush=True)

    def present():
        _ensure_hud().show(anchor, gloss)

    if _RUNLOOP is None:
        present()
        return
    Quartz.CFRunLoopPerformBlock(_RUNLOOP, Quartz.kCFRunLoopCommonModes, present)
    Quartz.CFRunLoopWakeUp(_RUNLOOP)


def translate_current_prompt() -> None:
    root = app_element()
    if root is None:
        print("Grammatisch is not running.", flush=True)
        return
    anchor = prompt_anchor(target_window(root))
    if anchor is None:
        print("t  no prompt word on this screen", flush=True)
        return
    if hud_is_visible() and _HUD.word == anchor["word"]:
        hide_hud()
        print("t  hidden", flush=True)
        return
    try:
        gloss, source = english_gloss(anchor["word"])
    except Exception as err:
        print(f"t  {anchor['word']}\n   lookup failed: {err}", flush=True)
        return
    show_gloss(anchor, gloss, source)


def translate_current_prompt_async() -> None:
    threading.Thread(target=translate_current_prompt, daemon=True).start()


def is_translate_hotkey(keycode, flags) -> bool:
    """Ctrl+T, no cmd/opt, so typing t / ergibt / ergebt is unaffected."""
    if keycode != KEYCODE_T:
        return False
    control = flags & Quartz.kCGEventFlagMaskControl
    command = flags & Quartz.kCGEventFlagMaskCommand
    option = flags & Quartz.kCGEventFlagMaskAlternate
    return bool(control) and not command and not option


# --------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------


def trusted(prompt: bool = False) -> bool:
    """Whether this process holds Accessibility permission."""
    if AXIsProcessTrustedWithOptions is None:
        return True  # can't check; let the tap fail loudly instead
    options = {kAXTrustedCheckOptionPrompt: True} if prompt else None
    return bool(AXIsProcessTrustedWithOptions(options))


PERMISSION_HELP = textwrap.dedent(
    """\
    This needs Accessibility permission, and macOS grants it to the program that
    launched the script -- normally Terminal, not Python.

        System Settings -> Privacy & Security -> Accessibility
        turn on Terminal (or whichever terminal you run this from)

    Then fully quit and reopen that terminal -- the grant is only picked up on
    launch -- and run this again.
    """
)


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------


def mode_probe() -> int:
    """Dump the AX tree. The Python equivalent of `entire contents of window 1`."""
    root = app_element()
    if root is None:
        print("Grammatisch is not running.")
        return 1
    if not trusted():
        print(PERMISSION_HELP)
        return 1

    printed = 0
    for element, depth in walk(target_window(root)):
        role = ax_get(element, "AXRole") or "?"
        label = ax_label(element)
        point = ax_point(element)
        size = ax_size(element)
        where = ""
        if point and size:
            where = f"  @({point[0]:.0f},{point[1]:.0f}) {size[0]:.0f}x{size[1]:.0f}"
        print(f"{'  ' * depth}{role}{'  ' + repr(label) if label else ''}{where}")
        printed += 1

    if printed <= 1:
        print("\nNo child elements exposed. Grammatisch may not support the")
        print("Accessibility API, in which case the hotkeys won't work either.")
        return 1
    return 0


def mode_check() -> int:
    ok = True

    app = running_app()
    if app is None:
        print("x  Grammatisch is not running.")
        ok = False
    else:
        print(f"ok Grammatisch is running (pid {app.processIdentifier()}).")

    if trusted(prompt=True):
        print("ok Accessibility permission granted.")
    else:
        print("x  No Accessibility permission.\n")
        print(PERMISSION_HELP)
        ok = False

    if app is not None and trusted():
        window = target_window(app_element())
        buttons = answer_buttons(window)
        if buttons:
            print(f"ok Found {len(buttons)} answer button(s):")
            for index, button in enumerate(buttons, 1):
                label = button["label"] or "(no label)"
                print(f"     {index}. {label}  @({button['x']:.0f},{button['y']:.0f})")
        else:
            lemma = prompt_lemma(window)
            if lemma:
                print(f"ok No answer buttons (text/conjugation screen). Prompt: {lemma}")
            else:
                print("x  No answer buttons found. Run --probe to see the AX tree.")
                ok = False

    return 0 if ok else 1


def mode_translate() -> int:
    if not trusted(prompt=True):
        print(PERMISSION_HELP)
        return 1
    root = app_element()
    if root is None:
        print("Grammatisch is not running.")
        return 1
    word = prompt_lemma(target_window(root))
    if not word:
        print("No prompt word on this screen. Run --probe to see the AX tree.")
        return 1
    try:
        gloss, source = english_gloss(word)
    except Exception as err:
        print(f"{word}\nlookup failed: {err}")
        return 1
    print(f"{word}\n{gloss}  [{source}]")
    return 0


def mode_listen() -> int:
    if not trusted(prompt=True):
        print(PERMISSION_HELP)
        return 1

    global _RUNLOOP
    NSApplication.sharedApplication()
    _RUNLOOP = Quartz.CFRunLoopGetCurrent()

    def handler(proxy, event_type, event, refcon):
        # macOS disables a tap that blocks for too long; re-arm it.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(tap, True)
            return event

        if event_type == Quartz.kCGEventLeftMouseDown and hud_is_visible():
            loc = Quartz.CGEventGetLocation(event)
            if _HUD.contains_cocoa_point(loc.x, loc.y):
                hide_hud()
                return None
            return event

        if event_type != Quartz.kCGEventKeyDown:
            return event

        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        flags = Quartz.CGEventGetFlags(event)

        if (
            keycode == KEYCODE_ESCAPE
            and hud_is_visible()
            and frontmost_is_target()
            and not (flags & BLOCKING_MODIFIERS)
        ):
            hide_hud()
            return None

        if is_translate_hotkey(keycode, flags):
            if frontmost_is_target():
                translate_current_prompt_async()
                return None
            return event

        slot = KEYCODE_TO_SLOT.get(keycode)
        if slot is None:
            return event

        if flags & BLOCKING_MODIFIERS:
            return event

        if not frontmost_is_target():
            return event

        root = app_element()
        if root is None:
            return event

        buttons = answer_buttons(target_window(root))
        if slot >= len(buttons):
            return event  # fewer options on screen than the key implies

        button = buttons[slot]
        if press(button):
            print(f"{slot + 1} -> {button['label'] or '(unlabelled button)'}", flush=True)
            return None  # swallow, so the digit isn't also typed into the app

        return event

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(
        Quartz.kCGEventLeftMouseDown
    )
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,  # default = we may suppress events
        mask,
        handler,
        None,
    )

    if not tap:
        print("Could not create the event tap.\n")
        print(PERMISSION_HELP)
        return 1

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(tap, True)

    print("Listening. 1 / 2 / 3 answer Grammatisch while it's frontmost.")
    print("Ctrl+T glosses the on-screen word. Esc or click the gloss to close.")
    print("Ctrl-C to quit.")
    try:
        Quartz.CFRunLoopRun()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Answer Grammatisch with 1/2/3, and gloss the prompt with Ctrl+T.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe", action="store_true", help="dump Grammatisch's AX tree")
    group.add_argument("--check", action="store_true", help="verify permissions and exit")
    group.add_argument(
        "--translate",
        action="store_true",
        help="look up the current prompt word and exit",
    )
    args = parser.parse_args()

    if args.probe:
        return mode_probe()
    if args.check:
        return mode_check()
    if args.translate:
        return mode_translate()
    return mode_listen()


if __name__ == "__main__":
    sys.exit(main())
