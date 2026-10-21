"""Verify the standard Gatekeeper policy on a vanilla lab image."""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys


def assessment_control(operation: str) -> bool:
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFErrorGetCode.argtypes = [ctypes.c_void_p]
    cf.CFErrorGetCode.restype = ctypes.c_long
    security.SecAssessmentControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecAssessmentControl.restype = ctypes.c_bool
    control = cf.CFStringCreateWithCString(None, operation.encode(), 0x08000100)
    if not control:
        raise RuntimeError("Could not create the Gatekeeper control name.")
    result = ctypes.c_void_p()
    error = ctypes.c_void_p()
    try:
        ok = security.SecAssessmentControl(
            control, ctypes.byref(result), ctypes.byref(error)
        )
        if not ok or error.value:
            code = cf.CFErrorGetCode(error) if error.value else "unknown"
            raise RuntimeError(f"Gatekeeper {operation} failed: CFError {code}")
        if not result.value:
            raise RuntimeError(f"Gatekeeper {operation} returned no policy state.")
        return bool(cf.CFBooleanGetValue(result))
    finally:
        cf.CFRelease(control)
        if error.value:
            cf.CFRelease(error)


def policy_state() -> dict[str, bool]:
    status = subprocess.run(
        ["spctl", "--status"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if status not in {"assessments enabled", "assessments disabled"}:
        raise RuntimeError(f"Unrecognized Gatekeeper status: {status!r}")
    return {
        "assessments_enabled": status == "assessments enabled",
        "developer_id_enabled": assessment_control("ui-get-devid"),
        "notarized_developer_id_enabled": assessment_control("ui-get-notarized"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This tool requires macOS.")
    state = policy_state()
    print(json.dumps(state, sort_keys=True))
    if not all(state.values()):
        print(
            "Gatekeeper is not using the standard identified-developer policy.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
