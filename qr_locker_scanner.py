"""
LastMeter — QR Code Locker Scanner
Full-screen UI with menu: Pick Up Package / Deliver Package

Setup (laptop or Pi):
    pip install opencv-python pyzbar requests numpy
    # Linux/Pi also needs the zbar shared library:
    # sudo apt install -y libzbar0

For the Pi servo:
    sudo apt install -y python3-gpiozero

Run:
    python qr_locker_scanner.py

Press Q in the window to quit. Press B or ESC from scanner to return to menu.
"""

import time
import cv2
import numpy as np
import requests
from pyzbar.pyzbar import decode

# ====== CONFIGURATION ======
BACKEND_URL = "http://10.10.10.72:8080"

CAMERA_INDEX = 0
SCAN_COOLDOWN_SECONDS = 3.0
FEEDBACK_DURATION_SECONDS = 4.0

SERVO_ENABLED = True
SERVO_GPIO_PIN = 27
SERVO_OPEN_SECONDS = 3

PICKUP_TIMEOUT_SECONDS = 60   # locker stays open for this long if nobody presses "Picked Up"
# ===========================

WINDOW_NAME = "LastMeter"

# Colours (BGR)
_BG      = (30, 30, 30)
_WHITE   = (255, 255, 255)
_GRAY    = (160, 160, 160)
_GREEN   = (40, 160, 40)
_GREEN_H = (60, 210, 60)
_BLUE    = (160, 80, 40)
_BLUE_H  = (210, 130, 70)
_GOLD    = (40, 160, 210)
_RED     = (50, 50, 200)

# --- Servo setup ---
_pwm = None
if SERVO_ENABLED:
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(SERVO_GPIO_PIN, GPIO.OUT)
        _pwm = GPIO.PWM(SERVO_GPIO_PIN, 50)
        _pwm.start(0)  # 0 duty cycle = no signal = no jitter at startup
    except Exception as e:
        print(f"[WARN] RPi.GPIO not available — servo disabled: {e}")
        SERVO_ENABLED = False


def _move_servo(angle: float):
    """Move to angle (degrees) then cut signal to prevent jitter."""
    if _pwm is not None:
        duty = 5 + (angle / 180) * 5  # 0°→5%(1ms), 90°→7.5%(1.5ms), 180°→10%(2ms)
        _pwm.ChangeDutyCycle(duty)
        time.sleep(1.0)
        _pwm.ChangeDutyCycle(0)  # cut signal — servo holds position without jitter
    else:
        print(f"  [SIM] Servo → {angle}°")


def start_servo():
    _move_servo(90)  # open position


def stop_servo():
    _move_servo(0)   # closed position


# --- Backend ---

def pickup_package(tracking_number: str) -> dict:
    url = f"{BACKEND_URL}/packages/pickup"
    try:
        resp = requests.post(url, json={"trackingNumber": tracking_number}, timeout=5)
    except requests.RequestException as e:
        return {"ok": False, "message": f"Backend unreachable: {e}", "locker": None}

    try:
        body = resp.json()
    except ValueError:
        body = {}

    if resp.status_code == 200:
        locker   = body.get("lockerNumber")
        building = body.get("buildingName") or ""
        receiver = (
            f"{body.get('receiverFirstName') or ''} "
            f"{body.get('receiverLastName') or ''}"
        ).strip()
        print(f"  Receiver:    {receiver or '(unassigned)'}")
        print(f"  Description: {body.get('description') or '(none)'}")
        return {
            "ok": True,
            "message": f"OPEN LOCKER {locker}" + (f"  |  {building}" if building else ""),
            "locker": locker,
        }
    if resp.status_code == 404:
        return {"ok": False, "message": f"Package not found: {tracking_number}", "locker": None}
    if resp.status_code == 409:
        current = body.get("currentStatus", "UNKNOWN")
        if current == "PICKED_UP":
            msg = "Already picked up"
        elif current in ("PENDING", "ASSIGNED_TO_LOCKER"):
            msg = "Not in a locker yet"
        else:
            msg = body.get("error") or f"Cannot pick up (status: {current})"
        return {"ok": False, "message": msg, "locker": None}
    return {
        "ok": False,
        "message": f"HTTP {resp.status_code}: {body.get('error') or ''}",
        "locker": None,
    }


# --- Mouse state ---

_mx, _my, _clicked = 0, 0, False

def _mouse_cb(event, x, y, _flags, _param):
    global _mx, _my, _clicked
    _mx, _my = x, y
    if event == cv2.EVENT_LBUTTONDOWN:
        _clicked = True


def _consume_click() -> bool:
    global _clicked
    if _clicked:
        _clicked = False
        return True
    return False


# --- Drawing helpers ---

def _text_center(frame, text: str, cx: int, cy: int, scale: float, color, thickness: int):
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    cv2.putText(frame, text, (cx - tw // 2, cy + th // 2),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _button(frame, label: str, x: int, y: int, w: int, h: int,
            base_color, hover_color) -> bool:
    hover = x <= _mx <= x + w and y <= _my <= y + h
    color = hover_color if hover else base_color
    cv2.rectangle(frame, (x, y), (x + w, y + h), color, -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), _WHITE, 2)
    _text_center(frame, label, x + w // 2, y + h // 2, 0.9, _WHITE, 2)
    return hover and _consume_click()


# --- Feedback banner ---

_feedback: dict = {"message": "", "expires": 0.0, "ok": True}


def _show_feedback(message: str, ok: bool):
    _feedback["message"] = message
    _feedback["expires"] = time.time() + FEEDBACK_DURATION_SECONDS
    _feedback["ok"] = ok


def _draw_feedback(frame):
    if time.time() > _feedback["expires"] or not _feedback["message"]:
        return
    h, w = frame.shape[:2]
    color = _GREEN if _feedback["ok"] else _RED
    cv2.rectangle(frame, (0, 0), (w, 72), color, -1)
    _text_center(frame, _feedback["message"], w // 2, 36, 1.0, _WHITE, 2)


# --- Pickup confirmation overlay ---

def _draw_confirm_overlay(frame, locker: str, remaining: int, sw: int, sh: int) -> bool:
    """
    Draw the open-locker confirmation panel on top of the camera frame.
    Returns True if the user pressed "Picked Up".
    """
    pw, ph = int(sw * 0.68), int(sh * 0.62)
    px, py = (sw - pw) // 2, (sh - ph) // 2

    overlay = frame.copy()
    cv2.rectangle(overlay, (px, py), (px + pw, py + ph), (15, 15, 15), -1)
    cv2.addWeighted(overlay, 0.88, frame, 0.12, 0, frame)
    cv2.rectangle(frame, (px, py), (px + pw, py + ph), _WHITE, 2)

    # Green header bar
    hh = int(ph * 0.22)
    cv2.rectangle(frame, (px, py), (px + pw, py + hh), _GREEN, -1)
    _text_center(frame, f"LOCKER {locker} IS OPEN",
                 sw // 2, py + hh // 2, 1.2, _WHITE, 3)

    # Body text
    _text_center(frame, "Your package is ready to be collected.",
                 sw // 2, py + int(ph * 0.38), 0.8, _WHITE, 2)
    _text_center(frame, "This QR code cannot be used again.",
                 sw // 2, py + int(ph * 0.50), 0.75, (80, 80, 220), 2)

    # Countdown
    color = _RED if remaining <= 10 else _GRAY
    _text_center(frame, f"Auto-closing in  {remaining}s",
                 sw // 2, py + int(ph * 0.63), 0.8, color, 2)

    # "Picked Up" button
    bw, bh = int(pw * 0.52), int(ph * 0.17)
    bx = sw // 2 - bw // 2
    by = py + int(ph * 0.76)
    return _button(frame, "Picked Up", bx, by, bw, bh, _GREEN, _GREEN_H)


# --- Screens ---

def show_menu(sw: int, sh: int) -> str:
    """Render the main menu. Returns 'pickup', 'deliver', or 'quit'."""
    cv2.setMouseCallback(WINDOW_NAME, _mouse_cb)

    bw = int(sw * 0.46)
    bh = int(sh * 0.13)
    bx = (sw - bw) // 2
    by1 = int(sh * 0.42)
    by2 = int(sh * 0.60)

    while True:
        frame = np.full((sh, sw, 3), _BG, dtype=np.uint8)

        _text_center(frame, "LastMeter", sw // 2, int(sh * 0.22), 2.8, _GOLD, 4)
        _text_center(frame, "Smart Locker System", sw // 2, int(sh * 0.33), 0.95, _GRAY, 2)

        if _button(frame, "1.  Pick Up Package", bx, by1, bw, bh, _GREEN, _GREEN_H):
            return "pickup"
        if _button(frame, "2.  Deliver Package", bx, by2, bw, bh, _BLUE, _BLUE_H):
            return "deliver"

        _text_center(frame, "Press 1 / 2 or click   |   Q to quit",
                     sw // 2, int(sh * 0.92), 0.6, _GRAY, 1)

        cv2.imshow(WINDOW_NAME, frame)
        key = cv2.waitKey(30) & 0xFF

        if key == ord('1'):
            return "pickup"
        if key == ord('2'):
            return "deliver"
        if key == ord('q'):
            return "quit"


def run_pickup_mode(sw: int, sh: int):
    """QR scanner for package pickup. Press B or ESC to return to menu."""
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print(f"ERROR: Could not open camera at index {CAMERA_INDEX}.")
        return

    cv2.setMouseCallback(WINDOW_NAME, _mouse_cb)
    last_scanned: dict[str, float] = {}
    picked_up: set[str] = set()  # tracking numbers already processed this session

    bbw, bbh = 140, 46
    bbx, bby = 20, 20

    # Two states: "scanning" — reading QR codes; "confirming" — locker is open
    state = "scanning"
    open_locker: str | None = None
    close_deadline = 0.0

    print("\n[Pickup mode] Hold a QR code up to the camera. B / ESC to go back.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to read frame from camera.")
                break

            frame = cv2.resize(frame, (sw, sh))

            if state == "scanning":
                for obj in decode(frame):
                    tracking_number = obj.data.decode("utf-8").strip()

                    pts = obj.polygon
                    if len(pts) == 4:
                        pts_list = [(p.x, p.y) for p in pts]
                        for i in range(4):
                            cv2.line(frame, pts_list[i], pts_list[(i + 1) % 4], (0, 255, 0), 3)
                    cv2.putText(frame, tracking_number,
                                (obj.rect.left, max(obj.rect.top - 10, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                    if tracking_number in picked_up:
                        continue
                    now = time.time()
                    if (tracking_number in last_scanned and
                            now - last_scanned[tracking_number] < SCAN_COOLDOWN_SECONDS):
                        continue
                    last_scanned[tracking_number] = now

                    print(f"\nScanned: {tracking_number}")
                    result = pickup_package(tracking_number)
                    print(f"  {result['message']}")

                    if result["ok"] and result["locker"]:
                        picked_up.add(tracking_number)
                        start_servo()
                        state = "confirming"
                        open_locker = result["locker"]
                        close_deadline = time.time() + PICKUP_TIMEOUT_SECONDS
                    else:
                        _show_feedback(result["message"], ok=False)

                _draw_feedback(frame)

                back_hover = bbx <= _mx <= bbx + bbw and bby <= _my <= bby + bbh
                cv2.rectangle(frame, (bbx, bby), (bbx + bbw, bby + bbh),
                              (80, 80, 80) if back_hover else (50, 50, 50), -1)
                cv2.rectangle(frame, (bbx, bby), (bbx + bbw, bby + bbh), _WHITE, 1)
                _text_center(frame, "< Back", bbx + bbw // 2, bby + bbh // 2, 0.7, _WHITE, 2)
                _text_center(frame, "Scan a QR code to pick up your package",
                             sw // 2, sh - 28, 0.7, _GRAY, 1)

                cv2.imshow(WINDOW_NAME, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord('b'), 27) or (back_hover and _consume_click()):
                    break

            elif state == "confirming":
                remaining = max(0, int(close_deadline - time.time()))
                picked_up = _draw_confirm_overlay(frame, open_locker, remaining, sw, sh)

                cv2.imshow(WINDOW_NAME, frame)
                cv2.waitKey(1)

                if picked_up or remaining <= 0:
                    stop_servo()
                    state = "scanning"
                    open_locker = None

    finally:
        stop_servo()
        cap.release()


def run_deliver_mode(_sw: int, _sh: int):
    """Placeholder — delivery flow to be implemented."""
    print("\n[Deliver mode] Coming soon.")
    # TODO: implement delivery flow


# --- Main ---

def main():
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    # Determine screen resolution from a temporary capture or fallback
    probe = cv2.VideoCapture(CAMERA_INDEX)
    sw = int(probe.get(cv2.CAP_PROP_FRAME_WIDTH))  or 1280
    sh = int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
    probe.release()

    print("=" * 50)
    print("LastMeter QR Pickup Scanner")
    print(f"Backend: {BACKEND_URL}")
    print(f"Servo:   {'ENABLED on GPIO ' + str(SERVO_GPIO_PIN) if SERVO_ENABLED else 'disabled (simulation)'}")
    print("=" * 50)

    while True:
        choice = show_menu(sw, sh)
        if choice == "pickup":
            run_pickup_mode(sw, sh)
        elif choice == "deliver":
            run_deliver_mode(sw, sh)
        elif choice == "quit":
            break

    cv2.destroyAllWindows()
    if _pwm is not None:
        try:
            _pwm.stop()
            GPIO.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    main()
