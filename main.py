from datetime import datetime, timedelta, date
import os
import keyboard
import tkinter as tk
import time
import platform
import ctypes
import threading
 
hardlock = True

LOCK_SECONDS = 300
USE_OS_LOCK = False

PRAYER_SCHEDULE = {
    "fajr": (6, 0),
    "dhuhr": (12, 30),
    "asr": (15, 45),
    "maghrib": (18, 15),
    "isha": (20, 0),
}

is_locked = False
lock_window = None
prayed_today = {name: False for name in PRAYER_SCHEDULE}
last_reset_date = date.today()
lock_mutex = threading.Lock()


def today_prayer_times():
    now = datetime.now()
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {name: base.replace(hour=h, minute=m) for name, (h, m) in PRAYER_SCHEDULE.items()}


def reset_if_new_day():
    global prayed_today, last_reset_date
    today = date.today()
    if today != last_reset_date:
        prayed_today = {name: False for name in PRAYER_SCHEDULE}
        last_reset_date = today
        print("New day — prayer reminders reset.")


def os_lock():
    if USE_OS_LOCK and platform.system() == "Windows":
        try:
            ctypes.windll.user32.LockWorkStation()
        except Exception as e:
            print("Could not call Windows lock:", e)


def show_lock_screen(duration_seconds):
    global lock_window, is_locked

    if lock_window is not None:
        return

    is_locked = True
    end_ts = time.time() + duration_seconds

    root = tk.Tk()
    lock_window = root
    root.title("Prayer Reminder")
    root.attributes("-topmost", True)
    root.attributes("-fullscreen", True)
    root.configure(bg="#111")
    root.protocol("WM_DELETE_WINDOW", lambda: None)

    if os.path.exists("background.png"):
        backgroundimage = tk.PhotoImage(file="background.png")
        background_label = tk.Label(root, image=backgroundimage)
        background_label.place(x=0, y=0, relwidth=1, relheight=1)
        background_label.image = backgroundimage
    else:
        root.configure(bg="#111")
        print("background.png not found, using solid color.")
    
    headline = tk.Label(root, text="Time for prayer", font=("Helvetica", 48), bg="#111", fg="#ffd166")
    headline.pack(pady=60)

    sub = tk.Label(root, text="Please pray now.", font=("Helvetica", 22), bg="#111", fg="#eee")
    sub.pack(pady=20)

    countdown = tk.Label(root, text="", font=("Helvetica", 20), bg="#111", fg="#ccc")
    countdown.pack(pady=10)

    def unlock():
        global is_locked, lock_window
        with lock_mutex:
            if not is_locked:
                return
            is_locked = False
            print("Unlocked — welcome back.")
            try:
                root.destroy()
            except Exception:
                pass
            lock_window = None

    if hardlock: 
        root.attributes("-disabled", True)
    else:
        root.attributes("-disabled", False)
        done_btn = tk.Button(root, text="Done", font=("Helvetica", 20), command=unlock)
        done_btn.pack(pady=30)

    def tick():
        remaining = int(end_ts - time.time())
        if remaining <= 0:
            unlock()
            return
        countdown.config(text=f"Unlocks in: {remaining} seconds")
        if is_locked:
            root.after(250, tick)

    tick()
    try:
        root.mainloop()
    except Exception:
        pass


def lock_now(manual=False):
    global is_locked
    with lock_mutex:
        if is_locked:
            print("Already locked.")
            return
        print("Locking for prayer — please pray.")
        os_lock()
        threading.Thread(target=show_lock_screen, args=(LOCK_SECONDS,), daemon=True).start()


def unlock_from_keyboard():
    global lock_window, is_locked
    with lock_mutex:
        if lock_window is not None and is_locked:
            print("Manual unlock via Tab.")
            is_locked = False
            try:
                lock_window.after(0, lock_window.destroy)
            except Exception:
                pass
            lock_window = None


def on_key(e):
    if e.event_type == "down":
        if e.name == "f10":
            print("Manual lock (F10  pressed).")
            lock_now(manual=True)
        elif e.name == "tab":
            unlock_from_keyboard()
        elif e.name == "f12":
            print("F12 pressed — exiting.")
            os._exit(0)

keyboard.hook(on_key)

if __name__ == "__main__":
    print("Prayer reminder running. Press Ctrl+C to quit.")
    try:
        while True:
            reset_if_new_day()
            now = datetime.now()
            times = today_prayer_times()
            for name, ptime in times.items():
                if not prayed_today[name]:
                    if ptime <= now < ptime + timedelta(seconds=60):
                        print(f"Reminder: {name} at {ptime.time()}")
                        prayed_today[name] = True
                        lock_now()
            time.sleep(1)
    except KeyboardInterrupt:
        print("Goodbye — reminder stopped.")
        with lock_mutex:
            if lock_window is not None:
                try:
                    lock_window.destroy()
                except Exception:
                    pass