"""
LastMeter — QR Code Locker Pickup Scanner
------------------------------------------
Scans a QR code containing a package's tracking number, hits the backend's
POST /packages/pickup endpoint to atomically mark the package as picked up
and free the locker, then (on the real Pi) drives a servo to open the
locker door.

Backend endpoint (from LastMeterBackend):
    POST http://localhost:8080/packages/pickup
    body:    { "trackingNumber": "ABC123" }
    200 OK:  PackageResponseDto { lockerNumber, buildingName, status=PICKED_UP, ... }
    404:     no package with that tracking number
    409:     wrong state (e.g. already picked up); body has { error, currentStatus }

Runs on both laptop (built-in webcam) and Raspberry Pi (USB webcam + servo).

Setup (laptop or Pi):
    pip install opencv-python pyzbar requests
    # Linux/Pi also needs the zbar shared library:
    # sudo apt install -y libzbar0

For the Pi servo (when you're ready):
    sudo apt install -y python3-gpiozero
    # then set SERVO_ENABLED = True below

Run:
    python qr_locker_scanner.py

Press 'q' in the camera window to quit.
"""

import time
import cv2
import requests
from pyzbar.pyzbar import decode

# ====== CONFIGURATION ======
BACKEND_URL = "http://localhost:8080"

# Camera index. On a laptop the built-in webcam is usually 0. If you plug in
# a USB webcam alongside the built-in one, the USB cam is often 1.
# On the Raspberry Pi with only a USB webcam, 0 is correct.
CAMERA_INDEX = 0

# Don't re-process the same QR within this many seconds
SCAN_COOLDOWN_SECONDS = 3.0

# How long the success/error banner stays on screen after a scan
FEEDBACK_DURATION_SECONDS = 4.0

# Servo settings — flip to True on the Raspberry Pi when you're ready
SERVO_ENABLED = False
SERVO_GPIO_PIN = 17       # BCM pin number
SERVO_OPEN_SECONDS = 3    # how long to hold the door open
# ===========================


# --- Servo setup (only imports gpiozero if enabled, so it stays cross-platform) ---
_servo = None
if SERVO_ENABLED:
    try:
        from gpiozero import Servo
        _servo = Servo(SERVO_GPIO_PIN)
    except ImportError:
        print("[WARN] gpiozero not installed — servo disabled. "
              "Install with: sudo apt install -y python3-gpiozero")
        SERVO_ENABLED = False


def open_locker_servo(locker_number: str):
    """Open the locker door for SERVO_OPEN_SECONDS, then close it."""
    print(f"  [SERVO] Opening locker {locker_number}")
    if SERVO_ENABLED and _servo is not None:
        _servo.max()
        time.sleep(SERVO_OPEN_SECONDS)
        _servo.min()
        print(f"  [SERVO] Locker {locker_number} closed")
    else:
        # On a laptop we just simulate
        print(f"  [SIM] (would hold servo open for {SERVO_OPEN_SECONDS}s)")


# --- Backend call ---

def pickup_package(tracking_number: str):
    """
    Call POST /packages/pickup. Returns a dict:
        { "ok": bool, "message": str, "locker": str|None }
    """
    url = f"{BACKEND_URL}/packages/pickup"
    try:
        resp = requests.post(url, json={"trackingNumber": tracking_number}, timeout=5)
    except requests.RequestException as e:
        return {"ok": False, "message": f"Backend unreachable: {e}", "locker": None}

    # Try to parse JSON even on errors — the backend returns JSON error bodies
    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code == 200:
        locker = body.get("lockerNumber")
        building = body.get("buildingName") or ""
        receiver = (
            f"{body.get('receiverFirstName') or ''} "
            f"{body.get('receiverLastName') or ''}"
        ).strip()
        print(f"  Receiver:    {receiver or '(unassigned)'}")
        print(f"  Description: {body.get('description') or '(none)'}")
        return {
            "ok": True,
            "message": f"OPEN LOCKER {locker}"
                       + (f" — {building}" if building else ""),
            "locker": locker,
        }

    if resp.status_code == 404:
        return {"ok": False,
                "message": f"Package not found: {tracking_number}",
                "locker": None}

    if resp.status_code == 409:
        current = body.get("currentStatus", "UNKNOWN")
        # Friendlier wording per status
        if current == "PICKED_UP":
            msg = "Already picked up"
        elif current in ("PENDING", "ASSIGNED_TO_LOCKER"):
            msg = "Not in a locker yet"
        else:
            msg = body.get("error") or f"Cannot pick up (status: {current})"
        return {"ok": False, "message": msg, "locker": None}

    # Anything else
    return {"ok": False,
            "message": f"HTTP {resp.status_code}: {body.get('error') or resp.text[:80]}",
            "locker": None}


# --- On-screen feedback banner ---

_feedback = {"message": "", "expires": 0.0, "ok": True}


def show_feedback(message: str, ok: bool):
    _feedback["message"] = message
    _feedback["expires"] = time.time() + FEEDBACK_DURATION_SECONDS
    _feedback["ok"] = ok


def draw_feedback(frame):
    if time.time() > _feedback["expires"] or not _feedback["message"]:
        return
    h, w = frame.shape[:2]
    color = (0, 180, 0) if _feedback["ok"] else (0, 0, 220)  # BGR: green / red
    # Filled banner at the top
    cv2.rectangle(frame, (0, 0), (w, 70), color, -1)
    cv2.putText(frame, _feedback["message"], (20, 47),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)


# --- Main loop ---

def main():
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"ERROR: Could not open camera at index {CAMERA_INDEX}.")
        print("If you have multiple cameras, try setting CAMERA_INDEX to 1.")
        return

    print("=" * 50)
    print("LastMeter QR Pickup Scanner")
    print(f"Backend: {BACKEND_URL}")
    print(f"Servo:   {'ENABLED on GPIO ' + str(SERVO_GPIO_PIN) if SERVO_ENABLED else 'disabled (simulation only)'}")
    print("Hold a QR code with a package tracking number up to the camera.")
    print("Press 'q' in the window to quit.")
    print("=" * 50)

    last_scanned = {}  # tracking_number -> last process time

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to read frame from camera.")
                break

            for obj in decode(frame):
                tracking_number = obj.data.decode("utf-8").strip()

                # Draw a green box around the QR
                pts = obj.polygon
                if len(pts) == 4:
                    pts_list = [(p.x, p.y) for p in pts]
                    for i in range(4):
                        cv2.line(frame, pts_list[i], pts_list[(i + 1) % 4],
                                 (0, 255, 0), 3)
                x, y = obj.rect.left, obj.rect.top
                cv2.putText(frame, tracking_number, (x, max(y - 10, 20)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

                # Cooldown: avoid hammering the backend with the same code
                now = time.time()
                if tracking_number in last_scanned \
                        and now - last_scanned[tracking_number] < SCAN_COOLDOWN_SECONDS:
                    continue
                last_scanned[tracking_number] = now

                print(f"\nScanned tracking number: {tracking_number}")
                result = pickup_package(tracking_number)
                print(f"  {result['message']}")
                show_feedback(result["message"], ok=result["ok"])

                if result["ok"] and result["locker"]:
                    open_locker_servo(result["locker"])

            draw_feedback(frame)
            cv2.imshow("LastMeter QR Pickup Scanner", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
