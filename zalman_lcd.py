#!/usr/bin/env python3
"""Linux driver + system-stats dashboard for the Zalman P40 DS / ZM-MF916 LCD (USB 1c57:3ed7).

Protocol (recovered from Zalman OZ-Wide 3.1, WideDisplay.cs / WideDisplay_HidSharp.cs):
  transport : one 1024-byte HID output report per frame/chunk (report ID 0)
  command   : 0x5A | len u16 BE (= payload + 5) | payload | sum(after 1st 0x5A) & 0xFF | 0x5A
              inside the markers 0x5A -> 5B 01 and 0x5B -> 5B 02
  payload   : "POST conn 1\r\nSeqNumber=2\r\nDate=<now>\r\n\r\n"
              "POST <name> 1\r\nSeqNumber=100\r\nContentType=json\r\nContentLength=<n>\r\n\r\n<json>"
              e.g. brightness {"value":10..100}, realtimeDisplay {"enable":true|false}
  JPEG frame: chunks of <=1000 bytes, each behind a 24-byte header
              5C | chunk_len+21 u16 BE | 01 | len//1000+1 u16 BE | index u16 BE (from 0) | 01 | 0...
              The panel is 1920x462; the app sends JPEG quality 85.

Usage:
  zalman_lcd.py info
  zalman_lcd.py brightness 80
  zalman_lcd.py image picture.png
  zalman_lcd.py off                       # leave real-time mode (back to the panel's own screen)
  zalman_lcd.py reboot                    # reboot the panel's firmware (fixes a stuck black screen)
  zalman_lcd.py dashboard [--fps 2] [--brightness 80]
  zalman_lcd.py snapshot out.png          # render one dashboard frame to a PNG (no panel needed)
"""
import argparse, collections, datetime, io, json, os, select, signal, socket, subprocess, time

DEV = "/dev/zalman-lcd"
W, H = 1920, 462
FRAME, ESC = 0x5A, 0x5B
CHUNK = 1000


# --------------------------------------------------------------------------- device
class ZalmanLCD:
    def __init__(self, path=DEV):
        self.fd = os.open(path, os.O_RDWR)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _write_report(self, data: bytes):
        os.write(self.fd, b"\x00" + data.ljust(1024, b"\x00"))

    @staticmethod
    def build_frame(payload: bytes) -> bytes:
        total = len(payload) + 5
        raw = bytearray([total >> 8, total & 0xFF]) + payload
        raw.append(sum(raw) & 0xFF)
        out = bytearray([FRAME])
        for b in raw:
            out += b"\x5b\x01" if b == FRAME else b"\x5b\x02" if b == ESC else bytes([b])
        out.append(FRAME)
        return bytes(out)

    @staticmethod
    def parse_frame(data: bytes):
        data = data.rstrip(b"\x00")
        if len(data) < 5 or data[0] != FRAME:
            return None
        inner = data[1:-1] if data[-1] == FRAME else data[1:]
        body, i = bytearray(), 0
        while i < len(inner):
            if inner[i] == ESC and i + 1 < len(inner):
                body.append(FRAME if inner[i + 1] == 1 else ESC)
                i += 2
            else:
                body.append(inner[i])
                i += 1
        return bytes(body[2:-1])

    def drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, 1025)

    def read_reply(self, timeout=1.5):
        end, buf = time.time() + timeout, b""
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                buf += os.read(self.fd, 1025).rstrip(b"\x00")
                if len(buf) > 4 and buf.endswith(bytes([FRAME])):
                    break
        if not buf:
            return None
        payload = self.parse_frame(buf)
        return payload.decode("utf-8", "replace") if payload is not None else buf.hex(" ")

    def send_text(self, text: str, timeout=1.5):
        self.drain()
        self._write_report(self.build_frame(text.encode()))
        return self.read_reply(timeout)

    def connect(self):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reply = self.send_text(f"POST conn 1\r\nSeqNumber=2\r\nDate={stamp}\r\n\r\n")
        if not reply or not reply.startswith("1 200"):
            raise IOError(f"connect failed: {reply!r}")
        return json.loads(reply.split("\r\n\r\n", 1)[1])

    def command(self, name: str, body: dict):
        js = json.dumps(body, separators=(",", ":"))
        reply = self.send_text(f"POST {name} 1\r\nSeqNumber=100\r\nContentType=json\r\n"
                               f"ContentLength={len(js.encode())}\r\n\r\n{js}")
        return reply.splitlines()[0] if reply else None

    def brightness(self, value: int):
        return self.command("brightness", {"value": max(10, min(100, int(value)))})

    def realtime(self, enable: bool):
        return self.command("realtimeDisplay", {"enable": bool(enable)})

    def heartbeat(self):
        # the app sends this every 500 ms, also while streaming; the panel does not answer it
        self._write_report(self.build_frame(b"STATE heartbeat 1\r\nSeqNumber=100\r\n\r\n"))

    def send_jpeg(self, jpg: bytes):
        count = len(jpg) // CHUNK + 1
        hdr = bytearray(24)
        hdr[0], hdr[3], hdr[8] = 0x5C, 1, 1
        hdr[4:6] = count.to_bytes(2, "big")
        for i, off in enumerate(range(0, len(jpg), CHUNK)):
            part = jpg[off:off + CHUNK]
            hdr[1:3] = (len(part) + 21).to_bytes(2, "big")
            hdr[6:8] = i.to_bytes(2, "big")
            self._write_report(bytes(hdr) + part)
        self.drain()


def to_jpeg(img, quality=85):
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=quality)
    return out.getvalue()


# --------------------------------------------------------------------------- stats
class Stats:
    def __init__(self, iface=None, hist=120):
        import psutil
        self.ps = psutil
        self.iface = iface
        self.hist = {k: collections.deque(maxlen=hist) for k in ("cpu", "gpu", "ram", "rx", "tx")}
        self.last_net = None
        self.gpu = {}
        self.gpu_t = 0
        psutil.cpu_percent(percpu=True)
        self.cpu_name = self._cpu_name()

    @staticmethod
    def _cpu_name():
        try:
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    n = line.split(":", 1)[1].strip()
                    for junk in ("AMD ", "Intel(R) ", "Core(TM) ", " Processor", " CPU"):
                        n = n.replace(junk, "")
                    return " ".join(n.split()[:3])
        except OSError:
            pass
        return "CPU"

    def _hwmon(self, chip, *labels):
        for name, entries in self.ps.sensors_temperatures().items():
            if name == chip:
                for e in entries:
                    if not labels or e.label in labels:
                        return e.current
                return entries[0].current if entries else None
        return None

    def _read_gpu(self):
        if time.time() - self.gpu_t < 1.0:
            return self.gpu
        self.gpu_t = time.time()
        try:
            q = "name,utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw,fan.speed,clocks.gr"
            out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout
            num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
            gpus = []
            for line in out.splitlines():
                f = [x.strip() for x in line.split(",")]
                gpus.append(dict(name=f[0].replace("NVIDIA GeForce ", "").replace("NVIDIA ", ""),
                                 util=num(f[1]) or 0, temp=num(f[2]), mem_used=num(f[3]), mem_total=num(f[4]),
                                 power=num(f[5]), fan=num(f[6]), clock=num(f[7])))
            # with several cards, show the one with the most VRAM (the main compute card)
            self.gpu = max(gpus, key=lambda g: g["mem_total"] or 0)
        except Exception:
            self.gpu = {}
        return self.gpu

    def sample(self):
        ps = self.ps
        s = {"cpu_name": self.cpu_name}
        s["cores"] = ps.cpu_percent(percpu=True)
        s["cpu"] = sum(s["cores"]) / max(1, len(s["cores"]))
        s["cpu_temp"] = self._hwmon("k10temp", "Tctl", "Tdie") or self._hwmon("coretemp", "Package id 0")
        s["vrm_temp"] = self._hwmon("asusec", "VRM")       # ASUS boards with the asus-ec-sensors driver
        fr = ps.cpu_freq()
        s["cpu_freq"] = fr.current if fr else None
        s["load"] = os.getloadavg()
        vm, sw = ps.virtual_memory(), ps.swap_memory()
        s["ram"], s["ram_used"], s["ram_total"] = vm.percent, vm.used, vm.total
        s["swap_used"], s["swap_total"] = sw.used, sw.total
        du = ps.disk_usage("/")
        s["disk"], s["disk_used"], s["disk_total"] = du.percent, du.used, du.total
        s["nvme_temp"] = self._hwmon("nvme", "Composite")
        s["gpu"] = self._read_gpu()

        nets = ps.net_io_counters(pernic=True)
        iface = self.iface or max((n for n in nets if n != "lo"), key=lambda n: nets[n].bytes_recv, default=None)
        now = time.time()
        rx = tx = 0.0
        if iface in nets:
            c = nets[iface]
            if self.last_net and self.last_net[0] == iface:
                dt = max(1e-3, now - self.last_net[1])
                rx = (c.bytes_recv - self.last_net[2]) / dt
                tx = (c.bytes_sent - self.last_net[3]) / dt
            self.last_net = (iface, now, c.bytes_recv, c.bytes_sent)
        s["iface"], s["rx"], s["tx"] = iface, rx, tx
        s["uptime"] = now - ps.boot_time()

        self.hist["cpu"].append(s["cpu"])
        self.hist["gpu"].append(s["gpu"].get("util", 0))
        self.hist["ram"].append(s["ram"])
        self.hist["rx"].append(rx)
        self.hist["tx"].append(tx)
        return s


# --------------------------------------------------------------------------- render
BG = (8, 10, 16)
PANEL = (18, 22, 32)
TRACK = (40, 46, 62)
TEXT = (235, 238, 245)
MUTED = (130, 138, 158)
ACCENT = {"cpu": (64, 160, 255), "gpu": (118, 214, 92), "ram": (190, 120, 255), "net": (255, 170, 60)}


def human_bytes(n, rate=False):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            s = f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
            return s + ("/s" if rate else "")
        n /= 1024


def heat(v, lo=50, hi=85):
    if v is None:
        return MUTED
    if v < lo:
        return TEXT
    if v < hi:
        return (255, 190, 70)
    return (255, 80, 80)


class Renderer:
    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont
        self.Image, self.ImageDraw = Image, ImageDraw
        def font(size, bold=False):
            names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
                     f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf"]
            for n in names:
                try:
                    return ImageFont.truetype(n, size)
                except OSError:
                    pass
            return ImageFont.load_default()
        self.f = {"title": font(30, True), "big": font(50, True), "mid": font(30, True), "txt": font(26),
                  "small": font(20), "clock": font(96, True)}
        self.host = socket.gethostname()

    def ring(self, d, cx, cy, r, pct, color, label):
        w = 22
        box = [cx - r, cy - r, cx + r, cy + r]
        d.arc(box, 135, 405, fill=TRACK, width=w)
        if pct:
            d.arc(box, 135, 135 + 270 * min(pct, 100) / 100, fill=color, width=w)
        d.text((cx, cy - 6), "--" if pct is None else f"{pct:.0f}%", font=self.f["big"], fill=TEXT, anchor="mm")
        d.text((cx, cy + 46), label, font=self.f["small"], fill=MUTED, anchor="mm")

    def spark(self, d, box, data, color, vmax=None):
        x0, y0, x1, y1 = box
        d.rounded_rectangle(box, 8, fill=(12, 15, 22))
        data = list(data)
        if len(data) < 2:
            return
        vmax = vmax or max(max(data), 1)
        n = data.__len__()
        step = (x1 - x0) / (n - 1)
        pts = [(x0 + i * step, y1 - (y1 - y0 - 4) * min(v, vmax) / vmax - 2) for i, v in enumerate(data)]
        fill = tuple(int(c * 0.35) for c in color)
        d.polygon([(x0, y1)] + pts + [(pts[-1][0], y1)], fill=fill)
        d.line(pts, fill=color, width=3)

    def lines(self, d, x, y, rows, gap=40):
        for k, v, col in rows:
            d.text((x, y), k, font=self.f["small"], fill=MUTED)
            d.text((x, y + 20), v, font=self.f["mid"], fill=col or TEXT)
            y += gap + 22

    def panel(self, d, x, title, sub, color, pw):
        d.rounded_rectangle([x, 10, x + pw, H - 10], 18, fill=PANEL)
        d.rounded_rectangle([x, 10, x + pw, 16], 3, fill=color)
        f = self.f["title"]
        while f.size > 16 and d.textlength(title, font=f) > pw - 44:
            f = f.font_variant(size=f.size - 2)
        d.text((x + 22, 30), title, font=f, fill=color)
        if sub:
            d.text((x + pw - 22, 36), sub, font=self.f["small"], fill=MUTED, anchor="ra")

    def render(self, s, hist):
        img = self.Image.new("RGB", (W, H), BG)
        d = self.ImageDraw.Draw(img)
        pw, gap = 466, 12
        xs = [10 + i * (pw + gap) for i in range(4)]

        # CPU
        x = xs[0]
        self.panel(d, x, "CPU", s["cpu_name"], ACCENT["cpu"], pw)
        self.ring(d, x + 125, 185, 95, s["cpu"], ACCENT["cpu"], "usage")
        self.lines(d, x + 255, 80, [
            ("Temp", f"{s['cpu_temp']:.0f} °C" if s["cpu_temp"] is not None else "n/a", heat(s["cpu_temp"], 75, 85)),
            ("Clock", f"{s['cpu_freq'] / 1000:.2f} GHz" if s["cpu_freq"] else "n/a", None),
            ("VRM", f"{s['vrm_temp']:.0f} °C", heat(s["vrm_temp"], 80, 100)) if s["vrm_temp"] is not None
            else ("Load", " ".join(f"{v:.1f}" for v in s["load"]), None)])
        cores = s["cores"]
        bx0, bx1, by0, by1 = x + 22, x + pw - 22, 310, 345
        bw = (bx1 - bx0) / max(1, len(cores))
        for i, c in enumerate(cores):
            cx = bx0 + i * bw
            d.rectangle([cx + 1, by0, cx + bw - 2, by1], fill=TRACK)
            d.rectangle([cx + 1, by1 - (by1 - by0) * c / 100, cx + bw - 2, by1], fill=ACCENT["cpu"])
        self.spark(d, [x + 22, 360, x + pw - 22, H - 26], hist["cpu"], ACCENT["cpu"], 100)

        # GPU
        x = xs[1]
        g = s["gpu"]
        self.panel(d, x, "GPU", g.get("name", "not found"), ACCENT["gpu"], pw)
        self.ring(d, x + 125, 185, 95, g.get("util", 0), ACCENT["gpu"], "usage")
        mem = (f"{g['mem_used'] / 1024:.1f} / {g['mem_total'] / 1024:.0f} GB"
               if g.get("mem_used") is not None else "n/a")
        self.lines(d, x + 255, 80, [
            ("Temp", f"{g['temp']:.0f} °C" if g.get("temp") is not None else "n/a", heat(g.get("temp"), 65, 83)),
            ("VRAM", mem, None),
            ("Power", f"{g['power']:.0f} W" if g.get("power") is not None else "n/a", None)])
        if g.get("mem_total"):
            frac = g["mem_used"] / g["mem_total"]
            d.rounded_rectangle([x + 22, 318, x + pw - 22, 338], 6, fill=TRACK)
            d.rounded_rectangle([x + 22, 318, x + 22 + (pw - 44) * frac, 338], 6, fill=ACCENT["gpu"])
        self.spark(d, [x + 22, 360, x + pw - 22, H - 26], hist["gpu"], ACCENT["gpu"], 100)

        # Memory / storage
        x = xs[2]
        self.panel(d, x, "MEMORY", f"{s['ram_total'] / 2**30:.0f} GB", ACCENT["ram"], pw)
        self.ring(d, x + 125, 185, 95, s["ram"], ACCENT["ram"], "RAM")
        self.lines(d, x + 255, 80, [
            ("Used", f"{s['ram_used'] / 2**30:.1f} GB", None),
            ("Swap", f"{s['swap_used'] / 2**30:.1f} GB", None),
            ("Disk /", f"{s['disk']:.0f}%  " + (f"{s['nvme_temp']:.0f}°C" if s["nvme_temp"] else ""),
             heat(s["disk"], 80, 92))])
        d.text((x + 22, 300), f"/  {human_bytes(s['disk_used'])} of {human_bytes(s['disk_total'])}",
               font=self.f["small"], fill=MUTED)
        d.rounded_rectangle([x + 22, 328, x + pw - 22, 344], 6, fill=TRACK)
        d.rounded_rectangle([x + 22, 328, x + 22 + (pw - 44) * s["disk"] / 100, 344], 6, fill=ACCENT["ram"])
        self.spark(d, [x + 22, 360, x + pw - 22, H - 26], hist["ram"], ACCENT["ram"], 100)

        # System: clock + network
        x = xs[3]
        now = datetime.datetime.now()
        self.panel(d, x, self.host.upper(), "", ACCENT["net"], pw)
        hm, ampm = now.strftime("%I:%M").lstrip("0"), now.strftime("%p")
        tw = d.textlength(hm, font=self.f["clock"]) + 12 + d.textlength(ampm, font=self.f["mid"])
        cx = x + (pw - tw) / 2
        d.text((cx, 125), hm, font=self.f["clock"], fill=TEXT, anchor="lm")
        d.text((cx + tw, 150), ampm, font=self.f["mid"], fill=MUTED, anchor="rs")
        d.text((x + pw / 2, 195), now.strftime("%A, %d %B"), font=self.f["txt"], fill=MUTED, anchor="mm")
        up = int(s["uptime"])
        d.text((x + pw / 2, 232), f"up {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m",
               font=self.f["small"], fill=MUTED, anchor="mm")
        d.text((x + 22, 268), f"▼ {human_bytes(s['rx'], True)}", font=self.f["mid"], fill=ACCENT["net"])
        d.text((x + pw - 22, 268), f"▲ {human_bytes(s['tx'], True)}", font=self.f["mid"], fill=(255, 110, 140),
               anchor="ra")
        d.text((x + 22, 312), s["iface"] or "", font=self.f["small"], fill=MUTED)
        vmax = max(max(hist["rx"], default=1), max(hist["tx"], default=1), 1024)
        self.spark(d, [x + 22, 340, x + pw - 22, H - 26], hist["rx"], ACCENT["net"], vmax)
        x0, y0, x1, y1 = x + 22, 340, x + pw - 22, H - 26
        tx = list(hist["tx"])
        if len(tx) > 1:
            step = (x1 - x0) / (len(tx) - 1)
            d.line([(x0 + i * step, y1 - (y1 - y0 - 4) * v / vmax - 2) for i, v in enumerate(tx)],
                   fill=(255, 110, 140), width=2)
        return img

# --------------------------------------------------------------------------- daemon
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def system_shutting_down():
    try:
        out = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "stopping"
    except (OSError, subprocess.SubprocessError):
        return False


CHECK_EVERY = 10.0      # seconds between panel state checks while streaming


def wake_panel(lcd, timeout=30):
    """Send "resume" and wait until the panel itself reports it is awake.

    The panel answers "resume" immediately but keeps waking for several seconds, during which it may
    not reply at all. Right after the PC powers on it can also fall back to the suspended state it was
    put in at shutdown, so require a few consecutive awake readings before trusting it.
    """
    log("power resume ->", lcd.command("power", {"event": "resume"}))
    end, awake = time.time() + timeout, 0
    while time.time() < end:
        time.sleep(1)
        try:
            info = lcd.connect()
        except OSError:
            awake = 0                      # busy while it wakes
            continue
        awake = awake + 1 if not info.get("suspend") else 0
        if awake >= 3:
            log("panel awake")
            return info
    raise IOError("panel did not wake from suspend")


def setup_panel(lcd, args):
    wake_panel(lcd)
    log("osd off ->", lcd.command("osdState", {"enable": False}))
    # the app never enables this; left on, the panel tries to keep displaying with no host
    log("display in sleep off ->", lcd.command("displayInSleep", {"enable": False}))
    if args.brightness:
        log("brightness", args.brightness, "->", lcd.brightness(args.brightness))
    for attempt in range(10):
        r = lcd.realtime(True)
        log("realtime on ->", r)
        if r and r.startswith("1 200"):
            return
        time.sleep(1)
    raise IOError("panel never accepted realtimeDisplay")


def check_panel(lcd, args, misses):
    """Between frames: make sure the panel hasn't gone back to sleep. Returns the new miss count."""
    try:
        info = lcd.connect()
    except OSError:
        if misses + 1 >= 3:
            raise IOError("panel stopped answering")
        return misses + 1
    if info.get("suspend") or (args.brightness and not info.get("brightness")):
        log(f"panel went dark (suspend={info.get('suspend')}, brightness={info.get('brightness')}) - waking it")
        setup_panel(lcd, args)
    return 0


def run_dashboard(args):
    stats = Stats(args.iface)
    renderer = Renderer()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    lcd, backoff = None, 2
    period = 1.0 / args.fps
    last_hb = last_check = 0.0
    misses = 0
    while not stop["flag"]:
        try:
            if lcd is None:
                lcd = ZalmanLCD(args.device)
                info = lcd.connect()
                log(f"connected: fw {info.get('version', {}).get('firmware')} sn {info.get('sn')}")
                setup_panel(lcd, args)
                backoff, last_check, misses = 2, time.time(), 0
            t0 = time.time()
            img = renderer.render(stats.sample(), stats.hist)
            if args.rotate:
                img = img.rotate(args.rotate, expand=True)
            lcd.send_jpeg(to_jpeg(img, args.quality))
            while True:                        # heartbeat every 500 ms, like the app
                if time.time() - last_hb >= 0.5:
                    lcd.heartbeat()
                    last_hb = time.time()
                left = period - (time.time() - t0)
                if left <= 0:
                    break
                time.sleep(min(left, 0.1))
            if time.time() - last_check >= CHECK_EVERY:   # between frames, so the reply isn't lost
                last_check = time.time()
                misses = check_panel(lcd, args, misses)
        except (OSError, IOError) as e:
            log("device error:", e, f"- retrying in {backoff}s")
            if lcd:
                lcd.close()
            lcd = None
            for _ in range(int(backoff * 10)):
                if stop["flag"]:
                    break
                time.sleep(0.1)
            backoff = min(backoff * 2, 30)
    if lcd:
        try:
            log("realtime off ->", lcd.realtime(False))
            # Like OZ-Wide on Windows shutdown: put the panel to sleep, so it waits for the next
            # "resume" instead of rebooting in a loop on standby power while the host is off.
            if system_shutting_down():
                log("shutdown: power suspend ->", lcd.command("power", {"event": "suspend"}))
        except OSError:
            pass
        lcd.close()
    log("stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["info", "brightness", "image", "off", "reboot", "dashboard", "snapshot"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--device", default=DEV)
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--brightness", type=int, default=0, help="10..100; 0 = leave unchanged")
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--rotate", type=int, default=0)
    ap.add_argument("--iface", default=None, help="network interface (default: busiest)")
    a = ap.parse_args()

    if a.action == "dashboard":
        return run_dashboard(a)
    if a.action == "snapshot":           # render one dashboard frame to a file, no device needed
        st = Stats(a.iface)
        st.sample(); time.sleep(1)
        Renderer().render(st.sample(), st.hist).save(a.args[0] if a.args else "snapshot.png")
        return

    lcd = ZalmanLCD(a.device)
    try:
        info = lcd.connect()
        if a.action == "info":
            print(json.dumps(info, indent=2))
        elif a.action == "brightness":
            print(lcd.brightness(int(a.args[0])))
        elif a.action == "off":
            print(lcd.realtime(False))
        elif a.action == "reboot":         # restarts the panel's own firmware; fixes a stuck black screen
            print(lcd.command("reboot", {"enable": True}))
        elif a.action == "image":
            from PIL import Image
            img = Image.open(a.args[0]).convert("RGB").resize((W, H))
            if a.rotate:
                img = img.rotate(a.rotate, expand=True)
            lcd.command("power", {"event": "resume"})
            print("realtime on ->", lcd.realtime(True))
            jpg = to_jpeg(img, a.quality)
            print("streaming image, Ctrl+C to stop")
            try:
                while True:
                    lcd.send_jpeg(jpg)
                    lcd.heartbeat()
                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("realtime off ->", lcd.realtime(False))
    finally:
        lcd.close()


if __name__ == "__main__":
    main()
