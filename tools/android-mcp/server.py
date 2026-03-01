#!/usr/bin/env python3.11
"""
MCP Server for Android app testing via ADB + UIAutomator + Debug HTTP Server.

Two-layer architecture:
- Layer 1: UIAutomator dump for accessibility tree + precise tap by bounds
- Layer 2: Debug HTTP server inside app for high-level commands (OpenChat, SendMessage, etc.)
"""

import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from PIL import Image
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CFG_PATH = Path(__file__).parent / "config.json"
_CFG = json.loads(_CFG_PATH.read_text()) if _CFG_PATH.exists() else {}

ADB_PATH = os.environ.get("ADB_PATH", _CFG.get("adb_path", "/opt/android-sdk/platform-tools/adb"))
DEVICE = os.environ.get("ANDROID_DEVICE", _CFG.get("device", "emulator-5554"))
DEBUG_PORT = int(os.environ.get("DEBUG_SERVER_PORT", _CFG.get("debug_server_port", 19876)))
SCREENSHOT_DIR = _CFG.get("screenshot_dir", "/tmp/android-screenshots")
DUMP_RETRIES = _CFG.get("uiautomator_dump_retries", 3)
DUMP_RETRY_DELAY = _CFG.get("uiautomator_dump_retry_delay", 1.0)

Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------

def _adb(args: str, timeout: int = 30) -> str:
    """Run adb command, return stdout."""
    cmd = f"{ADB_PATH} -s {DEVICE} {args}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 and "Error" in result.stderr:
        raise RuntimeError(f"adb error: {result.stderr.strip()}")
    return result.stdout.strip()


def _adb_raw(args: str, timeout: int = 30) -> bytes:
    """Run adb command, return raw bytes."""
    cmd = f"{ADB_PATH} -s {DEVICE} {args}"
    return subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout).stdout

# ---------------------------------------------------------------------------
# UIAutomator XML parsing
# ---------------------------------------------------------------------------

def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int]:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2)."""
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if not m:
        raise ValueError(f"Invalid bounds: {bounds_str}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def _center_of(bounds_str: str) -> tuple[int, int]:
    """Get center point of bounds."""
    x1, y1, x2, y2 = _parse_bounds(bounds_str)
    return (x1 + x2) // 2, (y1 + y2) // 2


def _dump_ui_tree() -> Optional[ET.Element]:
    """Dump UIAutomator hierarchy with retries for idle-state failures."""
    for attempt in range(DUMP_RETRIES):
        try:
            xml_str = _adb("shell uiautomator dump /dev/tty", timeout=10)
            # Remove the "UI hierarchy dumped to:" prefix line
            xml_str = re.sub(r'^UI hierarch?y dumped to:.*\n?', '', xml_str)
            if not xml_str.strip().startswith('<?xml') and not xml_str.strip().startswith('<hierarchy'):
                # Try extracting XML portion
                idx = xml_str.find('<hierarchy')
                if idx == -1:
                    idx = xml_str.find('<?xml')
                if idx >= 0:
                    xml_str = xml_str[idx:]
            return ET.fromstring(xml_str)
        except Exception:
            if attempt < DUMP_RETRIES - 1:
                time.sleep(DUMP_RETRY_DELAY * (attempt + 1))
    return None


def _walk_xml(node: ET.Element, depth: int = 0, max_depth: int = 15) -> list[str]:
    """Walk XML tree and produce text representation like qt_app_snapshot."""
    lines = []
    if depth > max_depth:
        return lines

    cls = node.get("class", "")
    text = node.get("text", "")
    res_id = node.get("resource-id", "")
    desc = node.get("content-desc", "")
    bounds = node.get("bounds", "")
    clickable = node.get("clickable", "false")
    enabled = node.get("enabled", "true")
    focused = node.get("focused", "false")

    # Short class name
    short_cls = cls.rsplit(".", 1)[-1] if cls else "?"

    indent = "  " * depth
    parts = [f"{indent}[{short_cls}]"]
    if res_id:
        rid = res_id.rsplit("/", 1)[-1] if "/" in res_id else res_id
        parts.append(f"@{rid}")
    if text:
        parts.append(f'"{text[:80]}"')
    if desc:
        parts.append(f'desc="{desc[:80]}"')

    states = []
    if clickable == "true":
        states.append("clickable")
    if focused == "true":
        states.append("focused")
    if enabled == "false":
        states.append("disabled")
    if states:
        parts.append(f"[{','.join(states)}]")
    if bounds:
        parts.append(bounds)

    lines.append(" ".join(parts))

    for child in node:
        lines.extend(_walk_xml(child, depth + 1, max_depth))

    return lines


def _find_node(root: ET.Element, resource_id: str = "", text: str = "",
               content_desc: str = "", class_name: str = "") -> Optional[ET.Element]:
    """Find first matching node in UI tree."""
    for node in root.iter():
        if resource_id:
            nid = node.get("resource-id", "")
            if resource_id not in nid:
                continue
        if text:
            ntext = node.get("text", "")
            if text.lower() not in ntext.lower():
                continue
        if content_desc:
            ndesc = node.get("content-desc", "")
            if content_desc.lower() not in ndesc.lower():
                continue
        if class_name:
            ncls = node.get("class", "")
            if class_name.lower() not in ncls.lower():
                continue
        return node
    return None

# ---------------------------------------------------------------------------
# Debug HTTP server helper (Layer 2)
# ---------------------------------------------------------------------------

_port_forwarded = False


def _ensure_port_forward():
    """Set up adb port forwarding for debug HTTP server."""
    global _port_forwarded
    if _port_forwarded:
        return
    try:
        _adb(f"forward tcp:{DEBUG_PORT} tcp:{DEBUG_PORT}")
        _port_forwarded = True
    except Exception as e:
        raise RuntimeError(f"Failed to set up port forwarding: {e}")


def _debug_call(endpoint: str, params: dict | None = None, timeout: int = 10) -> dict:
    """Call debug HTTP server endpoint. Returns parsed JSON response."""
    _ensure_port_forward()
    url = f"http://localhost:{DEBUG_PORT}/{endpoint}"
    if params:
        body = json.dumps(params).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except URLError as e:
        return {"ok": False, "error": f"Debug server unreachable: {e}. Is the app running with debug mode?"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "Invalid JSON response from debug server"}

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "android-cu",
    instructions=(
        "MCP server for Android app testing. Two layers:\n"
        "Layer 1: UIAutomator accessibility tree for system UI interaction.\n"
        "Layer 2: Debug HTTP server for high-level app commands (OpenChat, SendMessage, etc.).\n"
        "Note: Telegram's custom Canvas-drawn views (chat messages, dialog list) are NOT visible "
        "to UIAutomator. Use Layer 2 tools for core Telegram operations."
    ),
)

# ---------------------------------------------------------------------------
# Layer 1 MCP tools (UIAutomator + adb input)
# ---------------------------------------------------------------------------

@mcp.tool()
def android_snapshot(max_depth: int = 15) -> str:
    """Get the UI accessibility tree of the Android screen.

    Returns structured text tree showing all visible widgets with their class,
    resource-id, text, content-desc, bounds, and states.

    NOTE: Telegram's chat messages and dialog list are Canvas-drawn and will NOT
    appear in this tree. Use android_test_* tools for Telegram-specific operations.

    Args:
        max_depth: Maximum tree depth (default 15).
    """
    root = _dump_ui_tree()
    if root is None:
        return "ERROR: Failed to dump UI tree after retries. App may have animations blocking idle state."
    lines = _walk_xml(root, max_depth=max_depth)
    return "\n".join(lines) if lines else "UI tree is empty."


@mcp.tool()
def android_click(resource_id: str = "", text: str = "", content_desc: str = "") -> str:
    """Click a UI element by resource-id, text, or content-desc.

    Finds the element in UIAutomator tree and taps its center coordinates.
    At least one search parameter must be provided.

    Args:
        resource_id: Partial match on resource-id (e.g. "btn_send", "action_bar").
        text: Partial case-insensitive match on text content.
        content_desc: Partial case-insensitive match on content description.
    """
    if not resource_id and not text and not content_desc:
        return "ERROR: Provide at least one of resource_id, text, or content_desc."

    root = _dump_ui_tree()
    if root is None:
        return "ERROR: Failed to dump UI tree."

    node = _find_node(root, resource_id=resource_id, text=text, content_desc=content_desc)
    if node is None:
        return f"ERROR: Element not found (resource_id='{resource_id}', text='{text}', content_desc='{content_desc}'). Use android_snapshot to see available elements."

    bounds = node.get("bounds", "")
    if not bounds:
        return "ERROR: Element has no bounds."

    x, y = _center_of(bounds)
    _adb(f"shell input tap {x} {y}")

    desc = node.get("text") or node.get("content-desc") or node.get("resource-id", "?")
    return f"Tapped [{node.get('class', '?').rsplit('.', 1)[-1]}] '{desc}' at ({x}, {y})."


@mcp.tool()
def android_type(text: str, resource_id: str = "", element_text: str = "") -> str:
    """Type text into a field. Optionally find and tap the field first.

    If resource_id or element_text is provided, finds the element and taps it first.
    Then clears existing text and types the new text.

    Args:
        text: Text to type.
        resource_id: Optional resource-id to find and tap first.
        element_text: Optional existing text to find the field by.
    """
    if resource_id or element_text:
        root = _dump_ui_tree()
        if root:
            node = _find_node(root, resource_id=resource_id, text=element_text)
            if node:
                bounds = node.get("bounds", "")
                if bounds:
                    x, y = _center_of(bounds)
                    _adb(f"shell input tap {x} {y}")
                    time.sleep(0.3)

    # Select all + delete to clear
    _adb("shell input keyevent KEYCODE_MOVE_HOME")
    _adb("shell input keyevent --longpress KEYCODE_SHIFT_LEFT KEYCODE_MOVE_END")
    _adb("shell input keyevent KEYCODE_DEL")
    time.sleep(0.1)

    # Type text (escape special chars for shell)
    escaped = (text
               .replace("\\", "\\\\")
               .replace("\"", "\\\"")
               .replace(" ", "%s")
               .replace("&", "\\&")
               .replace("<", "\\<")
               .replace(">", "\\>")
               .replace("'", "\\'"))
    _adb(f'shell input text "{escaped}"')
    return f"Typed '{text}'."


@mcp.tool()
def android_tap(x: int, y: int) -> str:
    """Tap at exact screen coordinates.

    Args:
        x: X coordinate (pixels from left).
        y: Y coordinate (pixels from top).
    """
    _adb(f"shell input tap {x} {y}")
    return f"Tapped at ({x}, {y})."


@mcp.tool()
def android_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Swipe from one point to another.

    Args:
        x1: Start X. y1: Start Y. x2: End X. y2: End Y.
        duration_ms: Swipe duration in milliseconds (default 300).
    """
    _adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
    return f"Swiped from ({x1},{y1}) to ({x2},{y2}) in {duration_ms}ms."


@mcp.tool()
def android_press_key(key: str) -> str:
    """Press a key on the Android device.

    Args:
        key: Key name -- BACK, HOME, ENTER, TAB, DEL, MENU, SEARCH,
             DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT, or any KEYCODE_* name.
    """
    keycode = key if key.startswith("KEYCODE_") else f"KEYCODE_{key.upper()}"
    _adb(f"shell input keyevent {keycode}")
    return f"Key '{key}' pressed."


@mcp.tool()
def android_screenshot(filename: str = "") -> str:
    """Take a screenshot of the Android device screen.

    Args:
        filename: Output filename. Auto-generates if empty.

    Returns:
        Path to the saved screenshot file.
    """
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"android_{ts}.png"

    if not os.path.isabs(filename):
        filepath = os.path.join(SCREENSHOT_DIR, filename)
    else:
        filepath = filename

    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    # Capture screenshot via adb
    raw = _adb_raw("exec-out screencap -p", timeout=15)
    if not raw or len(raw) < 100:
        return "ERROR: Screenshot failed -- empty data returned."

    with open(filepath, "wb") as f:
        f.write(raw)

    # Downscale if any dimension exceeds 1920px (Claude limit is 2000px)
    MAX_DIM = 1920
    try:
        img = Image.open(filepath)
        w, h = img.size
        if w > MAX_DIM or h > MAX_DIM:
            scale = MAX_DIM / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            img.save(filepath, "PNG", optimize=True)
    except Exception:
        pass  # If resize fails, return original screenshot

    size = os.path.getsize(filepath)
    return f"Screenshot saved: {filepath} ({size} bytes)"


@mcp.tool()
def android_logcat(tag: str = "", lines: int = 50, level: str = "V") -> str:
    """Read Android logcat output.

    Args:
        tag: Filter by tag (e.g. "DebugTestServer", "Telegram"). Empty = all.
        lines: Number of recent lines (default 50).
        level: Minimum level: V(erbose), D(ebug), I(nfo), W(arn), E(rror). Default V.
    """
    cmd = f"shell logcat -d -t {lines}"
    if tag:
        cmd += f" -s {tag}:{level}"
    else:
        cmd += f" *:{level}"
    return _adb(cmd, timeout=10)


@mcp.tool()
def android_app_install(apk_path: str) -> str:
    """Install an APK on the device.

    Args:
        apk_path: Path to the APK file on the host.
    """
    if not os.path.exists(apk_path):
        return f"ERROR: APK not found: {apk_path}"
    return _adb(f"install -r -d {apk_path}", timeout=120)


@mcp.tool()
def android_app_launch(package: str, activity: str = "") -> str:
    """Launch an Android app by package name.

    Args:
        package: Package name (e.g. "co.topinnovations.run.beta").
        activity: Optional activity to launch. If empty, launches default.
    """
    if activity:
        return _adb(f"shell am start -n {package}/{activity}")
    else:
        return _adb(f"shell monkey -p {package} -c android.intent.category.LAUNCHER 1")


@mcp.tool()
def android_device_info() -> str:
    """Get connected device information."""
    try:
        model = _adb("shell getprop ro.product.model")
        sdk = _adb("shell getprop ro.build.version.sdk")
        abi = _adb("shell getprop ro.product.cpu.abi")
        size = _adb("shell wm size").replace("Physical size: ", "")
        density = _adb("shell wm density").replace("Physical density: ", "")
        return (
            f"Model: {model}\n"
            f"SDK: {sdk}\n"
            f"ABI: {abi}\n"
            f"Screen: {size} @ {density}dpi"
        )
    except Exception as e:
        return f"ERROR: {e}. Is device connected? Run: adb devices"

# ---------------------------------------------------------------------------
# Layer 2 MCP tools (Debug HTTP server)
# ---------------------------------------------------------------------------

@mcp.tool()
def android_test_open_chat(user_id: int) -> str:
    """Open a chat with a user by Telegram user ID.
    Uses the debug HTTP server inside the app -- no UI clicking needed.

    Args:
        user_id: Telegram user ID (e.g. 136907715 for PD).
    """
    result = _debug_call("openChat", {"userId": user_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_send_message(user_id: int, text: str) -> str:
    """Send a text message to a user. Opens chat if needed.

    Args:
        user_id: Telegram user ID.
        text: Message text to send.
    """
    result = _debug_call("sendMessage", {"userId": user_id, "text": text})
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_start_call(user_id: int, video: bool = False) -> str:
    """Start a voice or video call with a user.

    Args:
        user_id: Telegram user ID.
        video: If True, start video call. Default False (voice only).
    """
    result = _debug_call("startCall", {"userId": user_id, "video": video})
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_accept_call() -> str:
    """Accept an incoming call."""
    result = _debug_call("acceptCall")
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_end_call() -> str:
    """End the current active call."""
    result = _debug_call("endCall")
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_get_state() -> str:
    """Get the current app state as JSON.

    Returns call status, active chat info, current user, etc.
    """
    result = _debug_call("getState")
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_open_group(chat_id: int) -> str:
    """Open a group chat by chat ID.

    Args:
        chat_id: Telegram chat ID.
    """
    result = _debug_call("openGroup", {"chatId": chat_id})
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_send_code(phone: str) -> str:
    """Send verification code to a phone number for login.
    Uses the debug HTTP server to trigger auth.sendCode.
    For teamgram debug server, the magic code is '12345' if IP is whitelisted.

    Args:
        phone: Phone number without '+' (e.g. "16502859925").
    """
    result = _debug_call("sendCode", {"phone": phone}, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_sign_in(phone: str, code: str, phone_code_hash: str = "") -> str:
    """Sign in with verification code after calling send_code.

    Args:
        phone: Phone number without '+' (e.g. "16502859925").
        code: The verification code (e.g. "12345" for debug).
        phone_code_hash: Hash from send_code response. If empty, uses the last one.
    """
    params = {"phone": phone, "code": code}
    if phone_code_hash:
        params["phoneCodeHash"] = phone_code_hash
    result = _debug_call("signIn", params, timeout=20)
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_press_back() -> str:
    """Press the back button in the app (programmatic, not adb key)."""
    result = _debug_call("pressBack")
    return json.dumps(result, indent=2)


@mcp.tool()
def android_test_go_home() -> str:
    """Navigate to the main dialog list (home screen)."""
    result = _debug_call("goHome")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
