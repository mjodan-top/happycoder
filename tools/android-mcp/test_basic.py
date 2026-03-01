#!/usr/bin/env /opt/homebrew/bin/python3.11
"""Quick test script for Android MCP server functions."""
import json
import os
import sys
import subprocess
import re
import xml.etree.ElementTree as ET

# Set up paths
ADB_PATH = "/Users/fanmengni/android-sdk/platform-tools/adb"
DEVICE = "emulator-5554"

def _adb(args: str, timeout: int = 30) -> str:
    """Run adb command, return stdout."""
    cmd = f"{ADB_PATH} -s {DEVICE} {args}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 and "Error" in result.stderr:
        raise RuntimeError(f"adb error: {result.stderr.strip()}")
    return result.stdout.strip()

def _parse_bounds(bounds_str: str) -> tuple:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2)."""
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
    if not m:
        raise ValueError(f"Invalid bounds: {bounds_str}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

def _center_of(bounds_str: str) -> tuple:
    """Get center point of bounds."""
    x1, y1, x2, y2 = _parse_bounds(bounds_str)
    return (x1 + x2) // 2, (y1 + y2) // 2

def _dump_ui_tree() -> ET.Element:
    """Dump UIAutomator hierarchy."""
    # Use file instead of /dev/tty for more reliable output
    _adb("shell uiautomator dump /sdcard/Download/ui_dump.xml", timeout=10)
    xml_str = _adb("shell cat /sdcard/Download/ui_dump.xml", timeout=10)
    if not xml_str.strip().startswith('<?xml') and not xml_str.strip().startswith('<hierarchy'):
        idx = xml_str.find('<hierarchy')
        if idx == -1:
            idx = xml_str.find('<?xml')
        if idx >= 0:
            xml_str = xml_str[idx:]
    return ET.fromstring(xml_str)

def _walk_xml(node: ET.Element, depth: int = 0, max_depth: int = 5) -> list:
    """Walk XML tree and produce text representation."""
    lines = []
    if depth > max_depth:
        return lines

    cls = node.get("class", "")
    text = node.get("text", "")
    res_id = node.get("resource-id", "")
    desc = node.get("content-desc", "")
    bounds = node.get("bounds", "")

    short_cls = cls.rsplit(".", 1)[-1] if cls else "?"
    indent = "  " * depth
    parts = [f"{indent}[{short_cls}]"]
    if res_id:
        rid = res_id.rsplit("/", 1)[-1] if "/" in res_id else res_id
        parts.append(f"@{rid}")
    if text:
        parts.append(f'"{text[:40]}"')
    if bounds:
        parts.append(bounds)

    lines.append(" ".join(parts))

    for child in node:
        lines.extend(_walk_xml(child, depth + 1, max_depth))

    return lines

# Tests
print("=" * 60)
print("Android MCP Server - Quick Test")
print("=" * 60)

# Test 1: Device info
print("\n[Test 1] Device Info")
try:
    model = _adb("shell getprop ro.product.model")
    sdk = _adb("shell getprop ro.build.version.sdk")
    print(f"  Model: {model}")
    print(f"  SDK: {sdk}")
    print("  ✓ PASS")
except Exception as e:
    print(f"  ✗ FAIL: {e}")

# Test 2: Screenshot
print("\n[Test 2] Screenshot")
try:
    raw = subprocess.run(
        f"{ADB_PATH} -s {DEVICE} exec-out screencap -p",
        shell=True, capture_output=True, timeout=15
    ).stdout
    if raw and len(raw) > 100:
        print(f"  Screenshot size: {len(raw)} bytes")
        print("  ✓ PASS")
    else:
        print("  ✗ FAIL: Empty screenshot")
except Exception as e:
    print(f"  ✗ FAIL: {e}")

# Test 3: UIAutomator dump
print("\n[Test 3] UIAutomator Dump")
try:
    root = _dump_ui_tree()
    lines = _walk_xml(root, max_depth=3)
    print(f"  UI tree depth: {len(lines)} nodes")
    print("  Sample (first 10 lines):")
    for line in lines[:10]:
        print(f"    {line}")
    print("  ✓ PASS")
except Exception as e:
    print(f"  ✗ FAIL: {e}")

# Test 4: Tap
print("\n[Test 4] Tap (at 100, 100)")
try:
    _adb("shell input tap 100 100")
    print("  ✓ PASS")
except Exception as e:
    print(f"  ✗ FAIL: {e}")

print("\n" + "=" * 60)
print("All basic tests completed!")
print("=" * 60)