#!/usr/bin/env python3
"""
grammatisch_keys.py -- answer Grammatisch with the 1 / 2 / 3 keys.

Grammatisch ships mouse-only answer buttons. This adds the missing keyboard
layer: while Grammatisch is frontmost, pressing 1, 2 or 3 presses the first,
second or third answer button. Every other key, and every key in every other
app, passes through untouched.

Nothing is sent anywhere. No GUI app is installed. Two moving parts:

  * a CGEventTap that watches for bare 1/2/3 keydowns
  * the macOS Accessibility API, used to locate and press the buttons

Usage:
    python3 grammatisch_keys.py            # run the listener
    python3 grammatisch_keys.py --probe    # dump Grammatisch's AX tree
    python3 grammatisch_keys.py --check    # verify permissions and exit

Requires: pyobjc-framework-Quartz, pyobjc-framework-ApplicationServices
"""

from __future__ import annotations

import argparse
import sys
import textwrap

# --------------------------------------------------------------------------
# imports
# --------------------------------------------------------------------------

try:
    import Quartz
    from AppKit import NSWorkspace
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

# Bare keypresses only. If any of these modifiers are held, pass the key through
# so app and system shortcuts (cmd+1, ctrl+2, ...) keep working.
BLOCKING_MODIFIERS = (
    Quartz.kCGEventFlagMaskCommand
    | Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
)

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
        buttons = answer_buttons(target_window(app_element()))
        if buttons:
            print(f"ok Found {len(buttons)} answer button(s):")
            for index, button in enumerate(buttons, 1):
                label = button["label"] or "(no label)"
                print(f"     {index}. {label}  @({button['x']:.0f},{button['y']:.0f})")
        else:
            print("x  No answer buttons found. Run --probe to see the AX tree.")
            ok = False

    return 0 if ok else 1


def mode_listen() -> int:
    if not trusted(prompt=True):
        print(PERMISSION_HELP)
        return 1

    def handler(proxy, event_type, event, refcon):
        # macOS disables a tap that blocks for too long; re-arm it.
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(tap, True)
            return event

        if event_type != Quartz.kCGEventKeyDown:
            return event

        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        slot = KEYCODE_TO_SLOT.get(keycode)
        if slot is None:
            return event

        if Quartz.CGEventGetFlags(event) & BLOCKING_MODIFIERS:
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

    mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
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
        description="Answer Grammatisch with the 1 / 2 / 3 keys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--probe", action="store_true", help="dump Grammatisch's AX tree")
    group.add_argument("--check", action="store_true", help="verify permissions and exit")
    args = parser.parse_args()

    if args.probe:
        return mode_probe()
    if args.check:
        return mode_check()
    return mode_listen()


if __name__ == "__main__":
    sys.exit(main())
