#!/usr/bin/env python3
"""Automated RAM stress for Android devices over adb.

Drives real heavy apps (TikTok, YouTube ReVanced, YouTube, Maps, Camera, GMS apps),
opens many heavy Chrome tabs, keeps media playing in the background and launches
many apps at the same moment to push memory pressure. Device control only.

  python3 run_test.py --level-1 [-s SERIAL]    light   (~3 min)
  python3 run_test.py --level-2 [-s SERIAL]    medium  (~8 min)
  python3 run_test.py --level-3 [-s SERIAL]    heavy   (~15 min)

TikTok, YouTube ReVanced and its microG are installed when missing, from the APKs
next to this script (or --apk-dir).
Python 3 standard library only.
"""
import argparse
import http.server
import os
import re
import shlex
import socketserver
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
CONTENT_PORT = 8765
DEFAULT_APK_DIR = os.environ.get("RAMTEST_APK_DIR", HERE)
VIDEO_URL = "https://www.youtube.com/watch?v=jfKfPfyJRdk"

APPS = {
    "tiktok": ["com.zhiliaoapp.musically"],
    "revanced": ["app.revanced.android.youtube"],
    "youtube": ["com.google.android.youtube"],
    "chrome": ["com.android.chrome"],
    "maps": ["com.google.android.apps.maps"],
    "gmail": ["com.google.android.gm"],
    "photos": ["com.google.android.apps.photos"],
    "playstore": ["com.android.vending"],
    "camera": ["org.codeaurora.snapcam", "com.android.camera2", "com.google.android.GoogleCamera"],
    "calendar": ["com.google.android.calendar"],
    "files": ["com.google.android.apps.nbu.files"],
    "drive": ["com.google.android.apps.docs"],
    "ytmusic": ["com.google.android.apps.youtube.music"],
    "keep": ["com.google.android.keep"],
    "search": ["com.google.android.googlequicksearchbox"],
    "contacts": ["com.google.android.contacts"],
    "clock": ["com.google.android.deskclock"],
}

# microG first: ReVanced YouTube uses it in place of Google Play services
SIDELOAD = [
    ("app.revanced.android.gms", "youtube_revanced_micro_g.apk"),
    ("app.revanced.android.youtube", "youtube_revanced.apk"),
    ("com.zhiliaoapp.musically", "tiktok.apk"),
]

PERMS = ["CAMERA", "RECORD_AUDIO", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
         "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO", "POST_NOTIFICATIONS",
         "READ_CONTACTS", "READ_CALENDAR"]

FIRST_RUN_LABELS = {
    "agree and continue", "agree", "i agree", "accept", "accept all", "allow",
    "while using the app", "only this time", "skip", "not now", "no thanks", "no, thanks",
    "got it", "ok", "continue", "dismiss", "close", "maybe later", "use without an account",
    "start watching",
}


@dataclass
class Level:
    name: str
    apps: list            # heavy apps opened one by one (Chrome is driven by `tabs`)
    tabs: int
    tab_mb: int           # JS heap held by each tab
    feed_swipes: int
    dwell: float
    bg_media: bool        # ReVanced keeps playing in the background for the whole run
    burst: int            # apps force-stopped then launched at the same moment
    rounds: int           # app-switch rounds over everything opened


_L1 = ["tiktok", "revanced", "gmail", "maps", "playstore", "photos"]
_L2 = _L1 + ["youtube", "camera", "calendar", "files", "drive", "ytmusic"]
_L3 = _L2 + ["keep", "search", "contacts", "clock"]

LEVELS = {
    1: Level("light", _L1, tabs=6, tab_mb=30, feed_swipes=5, dwell=5,
             bg_media=False, burst=0, rounds=1),
    2: Level("medium", _L2, tabs=15, tab_mb=50, feed_swipes=8, dwell=4,
             bg_media=True, burst=6, rounds=2),
    3: Level("heavy", _L3, tabs=30, tab_mb=80, feed_swipes=12, dwell=3,
             bg_media=True, burst=len(_L3), rounds=3),
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- adb
class Device:
    def __init__(self, serial):
        self.serial = serial
        self.w, self.h = 1080, 2160
        self._comp = {}

    def adb(self, *args, timeout=120):
        cmd = ["adb", "-s", self.serial, *args]
        for _ in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                   errors="replace")
            except subprocess.TimeoutExpired:
                log(f"adb timeout: {' '.join(args)[:80]}")
                return ""
            if r.returncode != 0 and re.search(r"not found|offline|closed", r.stderr):
                self.reconnect()
                continue
            return r.stdout
        return ""

    def state(self):
        try:
            return subprocess.run(["adb", "-s", self.serial, "get-state"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except subprocess.TimeoutExpired:
            return ""

    def reconnect(self, wait=60):
        deadline = time.time() + wait
        while time.time() < deadline:
            if ":" in self.serial:
                subprocess.run(["adb", "connect", self.serial], capture_output=True, timeout=15)
            if self.state() == "device":
                return True
            time.sleep(3)
        return False

    def sh(self, cmd, timeout=120):
        return self.adb("shell", cmd, timeout=timeout)

    def prop(self, name):
        return self.sh(f"getprop {name}").strip()

    def screen_size(self):
        sizes = re.findall(r"(\d+)x(\d+)", self.sh("wm size"))
        if sizes:
            self.w, self.h = map(int, sizes[-1])

    def key(self, k):
        self.sh(f"input keyevent {k}")

    def swipe(self, x1, y1, x2, y2, ms=250):
        W, H = self.w, self.h
        self.sh(f"input swipe {int(x1*W)} {int(y1*H)} {int(x2*W)} {int(y2*H)} {ms}")

    def installed(self, pkg):
        return bool(self.sh(f"pm path {pkg}").strip())

    def component(self, pkg):
        if pkg not in self._comp:
            out = self.sh("cmd package resolve-activity --brief -a android.intent.action.MAIN "
                          f"-c android.intent.category.LAUNCHER {pkg}").strip().splitlines()
            c = out[-1].strip() if out else ""
            self._comp[pkg] = c if "/" in c else None
        return self._comp[pkg]

    def start(self, intent_args):
        out = self.sh(f"am start -W {intent_args}", timeout=60)
        res = {"state": None, "ms": None}
        m = re.search(r"LaunchState:\s*(\w+)", out)
        if m:
            res["state"] = m.group(1)
        m = re.search(r"TotalTime:\s*(\d+)", out)
        if m:
            res["ms"] = int(m.group(1))
        if "delivered to currently running top-most" in out:
            res["state"] = res["state"] or "TOP"
        if "Error" in out:
            res["error"] = out.strip().splitlines()[-1][:160]
        return res

    def mem(self):
        info = dict(re.findall(r"^(\w+):\s+(\d+) kB", self.sh("cat /proc/meminfo"), re.M))
        mb = lambda k: int(info.get(k, 0)) // 1024
        return f"MemAvailable {mb('MemAvailable')} MB, swap used {mb('SwapTotal') - mb('SwapFree')} MB"

    def tap_labels(self, labels, tries=4):
        """Tap buttons whose text/content-desc is one of `labels` (first-run, consent and
        permission screens) until none is left. Returns the number of taps."""
        taps = 0
        for _ in range(tries):
            xml = self.sh("uiautomator dump /data/local/tmp/ramtest_ui.xml >/dev/null 2>&1 && "
                          "cat /data/local/tmp/ramtest_ui.xml", timeout=30)
            i = xml.find("<hierarchy")
            if i < 0:
                return taps
            try:
                root = ET.fromstring(xml[i:])
            except ET.ParseError:
                return taps
            hit = None
            for n in root.iter("node"):
                label = (n.get("text") or n.get("content-desc") or "").strip().lower()
                if label in labels and n.get("enabled") != "false":
                    hit = re.findall(r"\d+", n.get("bounds", ""))
                    break
            if not hit or len(hit) != 4:
                return taps
            x1, y1, x2, y2 = map(int, hit)
            self.sh(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
            taps += 1
            time.sleep(1.5)
        return taps


def pick_serial(serial):
    if serial:
        return serial
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    devs = [p[0] for p in (l.split() for l in out.splitlines()[1:]) if len(p) >= 2 and p[1] == "device"]
    if len(devs) != 1:
        sys.exit(f"{len(devs)} devices online, pass -s SERIAL" if devs else "no device online (adb devices)")
    return devs[0]


# --------------------------------------------------------------------------- heavy web page
def start_content_server(port=CONTENT_PORT):
    root = os.path.join(HERE, "content", "web")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, *a):
            pass
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- setup
def install_sideloads(dev, apk_dir):
    for pkg, apk in SIDELOAD:
        if dev.installed(pkg):
            continue
        path = os.path.join(apk_dir, apk)
        if not os.path.isfile(path):
            log(f"install {pkg}: {path} not found - skipped")
            continue
        log(f"install {apk} ({os.path.getsize(path) >> 20} MB) ...")
        r = subprocess.run(["adb", "-s", dev.serial, "install", "-r", "-g", path],
                           capture_output=True, text=True, timeout=1800)
        out = (r.stdout + r.stderr).strip().splitlines()
        log(f"  {out[-1] if out else r.returncode}")
    # keeps ReVanced from stopping on a "microG is battery optimized" prompt
    dev.sh("cmd deviceidle whitelist +app.revanced.android.gms >/dev/null 2>&1")


class Session:
    """Device settings changed for the run, restored by close()."""

    def __init__(self, dev, pkgs):
        self.dev = dev
        self.old = {
            "system screen_off_timeout": dev.sh("settings get system screen_off_timeout").strip(),
            "global stay_on_while_plugged_in": dev.sh("settings get global stay_on_while_plugged_in").strip(),
        }
        dev.sh("input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard")
        dev.sh("settings put system screen_off_timeout 1800000; "
               "settings put global stay_on_while_plugged_in 15")
        dev.sh("; ".join(f"pm grant {p} android.permission.{perm} 2>/dev/null"
                         for p in pkgs for perm in PERMS) + "; true", timeout=300)
        flags = "_ --disable-fre --no-default-browser-check --no-first-run --disable-signin-promo"
        for f in ("/data/local/chrome-command-line", "/data/local/tmp/chrome-command-line"):
            dev.sh(f"echo {shlex.quote(flags)} > {f}; chmod 644 {f}")
        dev.sh("am set-debug-app --persistent com.android.chrome")
        self.srv = start_content_server()
        dev.adb("reverse", f"tcp:{CONTENT_PORT}", f"tcp:{CONTENT_PORT}")

    def close(self):
        d = self.dev
        d.key("KEYCODE_MEDIA_PAUSE")
        d.key("KEYCODE_HOME")
        for k, v in self.old.items():
            if v and v != "null":
                d.sh(f"settings put {k} {v}")
        d.sh("am clear-debug-app; rm -f /data/local/chrome-command-line /data/local/tmp/chrome-command-line")
        d.adb("reverse", "--remove", f"tcp:{CONTENT_PORT}")
        self.srv.shutdown()


# --------------------------------------------------------------------------- stress
class Stress:
    def __init__(self, dev, lv):
        self.dev, self.lv = dev, lv
        self.pkgs = {}
        self.opened = []
        self.cold_relaunch = {}
        self.launch_count = 0
        self.tab = 0
        for key in dict.fromkeys(lv.apps + ["chrome"]):
            pkg = next((p for p in APPS[key] if dev.component(p)), None)
            if pkg:
                self.pkgs[key] = pkg
            else:
                log(f"skip {key}: not installed")

    def launch(self, key, intent=None):
        pkg = self.pkgs.get(key)
        if not pkg:
            return None
        r = self.dev.start(intent + f" -p {pkg}" if intent else f"-n {shlex.quote(self.dev.component(pkg))}")
        self.launch_count += 1
        first = key not in self.opened
        if first:
            self.opened.append(key)
        elif r["state"] == "COLD":
            self.cold_relaunch[key] = self.cold_relaunch.get(key, 0) + 1
        log(f"  {key:<9} {r['state'] or '?':<5} {r['ms'] if r['ms'] is not None else '-':>5} ms"
            + (f"  {r['error']}" if "error" in r else ""))
        if first:
            time.sleep(2)
            n = self.dev.tap_labels(FIRST_RUN_LABELS)
            if n:
                log(f"  {key}: tapped {n} first-run button(s)")
        return r

    def scroll(self, n, gap=0.8, direction="up"):
        moves = {"up": (0.5, 0.75, 0.5, 0.3), "down": (0.5, 0.3, 0.5, 0.75)}
        for i in range(n):
            if direction == "pan":
                dx, dy = [(0.3, 0.0), (0.0, 0.25), (-0.3, 0.0), (0.0, -0.25)][i % 4]
                self.dev.swipe(0.5, 0.5, 0.5 + dx, 0.5 + dy, 400)
            else:
                self.dev.swipe(*moves[direction], 300)
            time.sleep(gap)

    def video_intent(self):
        return f"-a android.intent.action.VIEW -d {shlex.quote(VIDEO_URL)}"

    def open_app(self, key):
        lv = self.lv
        if key in ("revanced", "youtube"):
            if not self.launch(key, self.video_intent()):
                return
            time.sleep(lv.dwell * 2)
            self.scroll(lv.feed_swipes // 3, 1.0)
        elif key == "tiktok":
            if not self.launch(key):
                return
            time.sleep(lv.dwell)
            self.scroll(lv.feed_swipes, 2.5)
        elif key == "maps":
            if not self.launch(key):
                return
            time.sleep(lv.dwell)
            self.scroll(lv.feed_swipes, 0.6, "pan")
        elif key == "camera":
            if not self.launch(key):
                return
            time.sleep(lv.dwell)
            for _ in range(2):
                self.dev.key("KEYCODE_CAMERA")
                time.sleep(2)
        else:
            if not self.launch(key):
                return
            time.sleep(lv.dwell / 2)
            self.scroll(max(2, lv.feed_swipes // 2), 0.6)
        self.dev.key("KEYCODE_HOME")
        time.sleep(0.5)

    def chrome_tabs(self):
        lv = self.lv
        if "chrome" not in self.pkgs:
            return
        log(f"chrome: {lv.tabs} tabs x {lv.tab_mb} MB")
        for _ in range(lv.tabs):
            self.tab += 1
            url = f"http://127.0.0.1:{CONTENT_PORT}/heavy.html?id={self.tab}&mb={lv.tab_mb}&dom=3000&canvas=3"
            self.launch("chrome", f"-a android.intent.action.VIEW -d {shlex.quote(url)} "
                                  f"--es com.android.browser.application_id ramtest{self.tab} "
                                  "--ez create_new_tab true")
            time.sleep(lv.dwell / 2)
            self.scroll(2, 0.6)
        self.dev.key("KEYCODE_HOME")

    def burst(self):
        keys = [k for k in self.lv.apps[: self.lv.burst] if k in self.pkgs]
        if self.lv.bg_media:
            keys = [k for k in keys if k != "revanced"]
        if not keys:
            return
        log(f"burst: {len(keys)} apps launched at once ({', '.join(keys)})")
        pkgs = [self.pkgs[k] for k in keys]
        self.dev.sh("; ".join(f"am force-stop {p}" for p in pkgs))
        self.dev.sh(" ".join(f"am start -n {shlex.quote(self.dev.component(p))} >/dev/null 2>&1 &"
                             for p in pkgs) + " wait", timeout=180)
        time.sleep(self.lv.dwell * 3)
        log(f"  {self.dev.mem()}")
        self.dev.key("KEYCODE_HOME")

    def background_media(self):
        if "revanced" not in self.pkgs:
            log("background media: ReVanced not installed - skipped")
            return
        log("background media: ReVanced video, then HOME (background playback)")
        self.launch("revanced", self.video_intent())
        time.sleep(8)
        self.dev.key("KEYCODE_HOME")

    def switch_rounds(self):
        for r in range(1, self.lv.rounds + 1):
            log(f"switch round {r}/{self.lv.rounds}: {len(self.opened)} apps")
            for key in list(self.opened):
                self.launch(key)
                self.scroll(1, 0.4)
                time.sleep(self.lv.dwell / 2)
                if r % 2 == 0:
                    self.dev.key("KEYCODE_APP_SWITCH")
                    time.sleep(0.8)
            self.dev.key("KEYCODE_HOME")

    def run(self, loops):
        for i in range(1, loops + 1):
            log(f"=== loop {i}/{loops}  {self.dev.mem()}")
            if self.lv.burst:
                self.burst()
            if self.lv.bg_media:
                self.background_media()
            log(f"heavy apps: {len(self.lv.apps)}")
            for key in self.lv.apps:
                self.open_app(key)
            self.chrome_tabs()
            log(f"  {self.dev.mem()}")
            self.switch_rounds()
            log(f"=== loop {i} done  {self.dev.mem()}")

    def summary(self):
        log(f"launches: {self.launch_count}, apps opened: {len(self.opened)}")
        if self.cold_relaunch:
            log("COLD relaunch (process was gone while in background): " +
                ", ".join(f"{k} x{n}" for k, n in sorted(self.cold_relaunch.items(), key=lambda x: -x[1])))
        else:
            log("COLD relaunch: none")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    for n, lv in LEVELS.items():
        g.add_argument(f"--level-{n}", dest="level", action="store_const", const=n,
                       help=f"{lv.name}: {len(lv.apps)} apps, {lv.tabs} tabs x {lv.tab_mb} MB, "
                            f"burst {lv.burst}, {lv.rounds} switch round(s)"
                            + (", background video" if lv.bg_media else ""))
    ap.add_argument("-s", "--serial", help="adb serial (default: the only online device)")
    ap.add_argument("--loops", type=int, default=1, help="repeat the level N times (default 1)")
    ap.add_argument("--apk-dir", default=DEFAULT_APK_DIR,
                    help=f"folder with {', '.join(a for _, a in SIDELOAD)} (default {DEFAULT_APK_DIR})")
    ap.add_argument("--no-install", dest="install", action="store_false",
                    help="do not install TikTok / ReVanced when missing")
    args = ap.parse_args()

    lv = LEVELS[args.level]
    dev = Device(pick_serial(args.serial))
    log(f"device {dev.serial} {dev.prop('ro.product.model')} build {dev.prop('ro.build.display.id')}")
    log(f"level {args.level} ({lv.name}), loops {args.loops}")
    if args.install:
        install_sideloads(dev, args.apk_dir)
    dev.screen_size()

    stress = Stress(dev, lv)
    session = Session(dev, stress.pkgs.values())
    t0 = time.time()
    try:
        stress.run(args.loops)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        session.close()
        stress.summary()
        log(f"done in {(time.time() - t0) / 60:.1f} min  {dev.mem()}")


if __name__ == "__main__":
    main()
