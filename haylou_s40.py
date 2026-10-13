"""Haylou S40 para Windows: batería y modo (ANC / Normal / Transparencia).

Ventana pequeña + icono en la bandeja. Hablamos por Bluetooth LE con los
auriculares mientras siguen conectados como audio normal.
"""
import asyncio
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import ttk

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from PIL import Image, ImageDraw, ImageFont
import pystray

import s40_protocol as P

APP_NAME = "Haylou S40"
APP_DIR = os.path.join(os.environ.get("APPDATA", "."), "HaylouS40")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

MODE_NAMES = {P.ANC_NORMAL: "Off", P.ANC_ANC: "Cancelación de ruido (ANC)", P.ANC_ADAPTIVE: "ANC adaptativo", P.ANC_TRANSP: "Sonido ambiente"}
MODE_COLOR = {P.ANC_NORMAL: "#8E8E93", P.ANC_ANC: "#0A84FF", P.ANC_ADAPTIVE: "#5E5CE6", P.ANC_TRANSP: "#30D158"}



# ───────────────────────── configuración ─────────────────────────
def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --bandeja'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --bandeja'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "HaylouS40")
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, "HaylouS40", 0, winreg.REG_SZ, exe_command())
        else:
            try:
                winreg.DeleteValue(k, "HaylouS40")
            except OSError:
                pass


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "HaylouS40_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


# ───────────────────────── Bluetooth ─────────────────────────
SETTABLE = {"anc": P.ATTR_ANC, "game": P.ATTR_GAME, "wind": P.ATTR_WIND,
            "spatial": P.ATTR_SPATIAL, "multi": P.ATTR_MULTI, "scene": P.ATTR_SCENE}


class Headset:
    """Hilo con su propio asyncio loop. El estado se lee desde la UI sin bloquear."""

    def __init__(self):
        self.cfg = load_config()
        self.state = {"connected": False, "status": "Buscando auriculares…",
                      "battery": None, "anc": None, "game": None, "wind": None,
                      "spatial": None, "multi": None, "scene": None}
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.client = None
        self.run = True
        self.last_rx = 0.0
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True).start()

    # --- API para la UI ---
    def send(self, kind, value=None):
        self.q.put((kind, value))

    def stop(self):
        self.run = False
        self.q.put(("quit", None))

    # --- internos ---
    def _status(self, text, connected=None):
        self.state["status"] = text
        if connected is not None:
            self.state["connected"] = connected

    def _on_notify(self, _, data):
        b = bytes(data)
        self.last_rx = time.time()
        pct = P.parse_battery(b)
        if pct is not None:
            self.state["battery"] = pct
        info = P.parse_run_info(b)
        if info:
            for key, o in (("anc", P.ORD_ANC), ("game", P.ORD_GAME), ("wind", P.ORD_WIND),
                           ("spatial", P.ORD_SPATIAL), ("multi", P.ORD_MULTI),
                           ("scene", P.ORD_SCENE)):
                v = info.get(o)
                if v is not None and v != 0xFF:
                    self.state[key] = v
        elif pct is None and P.is_valid(b) and b[4] not in (0x02, 0x08, 0x09):
            # aviso espontáneo (p. ej. botón físico): pedimos el estado completo
            self.q.put(("refresh", None))

    async def _find(self):
        addr = self.cfg.get("addr")
        if addr:
            dev = await BleakScanner.find_device_by_address(addr, timeout=5.0)
            if dev:
                return dev
            return BLEDevice(addr, "HAYLOU S40", None)
        for d in await BleakScanner.discover(timeout=6.0):
            if (d.name or "").upper().startswith("HAYLOU S40"):
                return d
        return None

    async def _connect(self):
        self._status("Buscando auriculares…", False)
        dev = await self._find()
        if dev is None:
            self._status("No encuentro los auriculares. ¿Están encendidos y conectados?", False)
            return False
        client = None
        names = set()
        for fresh in (False, True):  # 2.º intento: sin la caché de servicios de Windows
            client = BleakClient(dev, timeout=15.0, winrt={"use_cached_services": False} if fresh else {})
            await client.connect()
            names = {c.uuid.lower() for s in client.services for c in s.characteristics}
            if P.SVC_WRITE in names:
                break
            await client.disconnect()
            await asyncio.sleep(1.5)
        if P.SVC_WRITE not in names:
            self._status("Los auriculares no responden. ¿Hay otra app (o el celular) controlándolos?", False)
            return False
        for uuid in P.SVC_NOTIFY:
            if uuid in names:
                await client.start_notify(uuid, self._on_notify)
        self.client = client
        if self.cfg.get("addr") != dev.address:
            self.cfg["addr"] = dev.address
            save_config(self.cfg)
        self._status("Conectado", True)
        return True

    def _alive(self):
        return self.client is not None and self.client.is_connected

    async def _write(self, frame):
        await self.client.write_gatt_char(P.SVC_WRITE, frame, response=False)

    async def _refresh(self, battery=True):
        if battery:
            await self._write(P.build_get_battery())
            await asyncio.sleep(0.25)
        await self._write(P.build_get_all())

    async def _drop(self):
        try:
            if self.client:
                await self.client.disconnect()
        except Exception:
            pass
        self.client = None
        self.state["connected"] = False

    async def _main(self):
        loop = asyncio.get_running_loop()
        last_poll = last_batt = 0.0
        last_try = 0.0
        while self.run:
            if not self._alive():
                if self.state["connected"]:
                    self._status("Conexión perdida, reconectando…", False)
                if time.time() - last_try >= 8:
                    last_try = time.time()
                    try:
                        if await self._connect():
                            await self._refresh()
                            last_poll = last_batt = time.time()
                    except Exception as e:
                        await self._drop()
                        self._status(f"No se pudo conectar ({type(e).__name__}). Reintentando…", False)
            try:
                kind, value = await loop.run_in_executor(None, lambda: self.q.get(timeout=0.5))
            except queue.Empty:
                kind = None
            if kind == "quit":
                break
            try:
                if kind == "reconnect":
                    await self._drop()
                    last_try = 0
                elif self._alive():
                    if kind in SETTABLE:
                        self.state[kind] = int(value)
                        await self._write(P.build_set(SETTABLE[kind], int(value)))
                    if kind in SETTABLE or kind == "refresh":
                        await asyncio.sleep(0.9)
                        await self._refresh(battery=False)
                    now = time.time()
                    if now - last_poll >= 5:
                        last_poll = now
                        await self._refresh(battery=now - last_batt >= 30)
                        if now - last_batt >= 30:
                            last_batt = now
            except Exception:
                await self._drop()
        await self._drop()


# ───────────────────────── icono de bandeja ─────────────────────────
def make_icon(battery, anc, connected=True) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = MODE_COLOR.get(anc, "#8A8F98") if connected else "#4B5058"
    d.ellipse((2, 2, 62, 62), fill=color)
    text = "?" if battery is None or not connected else str(battery)
    try:
        font = ImageFont.truetype("arialbd.ttf", 30 if len(text) < 3 else 24)
    except Exception:
        font = ImageFont.load_default()
    d.text((32, 33), text, fill="white", font=font, anchor="mm")
    return img


def app_icon_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "icon.ico")


# ───────────────────────── interfaz ─────────────────────────
# Un solo Canvas dibujado a mano: formas suaves (PIL, cacheadas), resortes críticamente
# amortiguados para los selectores, los interruptores y la barra. Solo se anima
# mientras algo se mueve; en reposo la app apenas consume.
import base64
import io
import math
from tkinter import font as tkfont

PALETTES = {
    "light": dict(bg="#F2F2F7", card="#FFFFFF", text="#1D1D1F", dim="#6E6E73", track="#E3E3E8",
                  thumb="#FFFFFF", hair="#E5E5EA", off="#D1D1D6", accent="#007AFF",
                  green="#34C759", orange="#FF9500", red="#FF3B30", press="#EBEBF0"),
    "dark": dict(bg="#000000", card="#1C1C1E", text="#F5F5F7", dim="#8E8E93", track="#1C1C1E",
                 thumb="#636366", hair="#38383A", off="#39393D", accent="#0A84FF",
                 green="#30D158", orange="#FF9F0A", red="#FF453A", press="#2C2C2E"),
}

# Selectores segmentados: clave de estado, valores en orden de pantalla, etiquetas, descripciones
SEGMENTS = {
    "anc": dict(
        values=[P.ANC_ANC, P.ANC_ADAPTIVE, P.ANC_NORMAL, P.ANC_TRANSP],
        labels={P.ANC_ANC: "ANC", P.ANC_ADAPTIVE: "Adaptativo", P.ANC_NORMAL: "Off",
                P.ANC_TRANSP: "Ambiente"},
        caption={P.ANC_ANC: "Cancelación activa de ruido",
                 P.ANC_ADAPTIVE: "ANC adaptativo: se ajusta a tu entorno",
                 P.ANC_NORMAL: "Sin procesamiento de sonido",
                 P.ANC_TRANSP: "Escuchás el sonido de tu entorno"}),
    "spatial": dict(
        values=[P.SPATIAL_DYNAMIC, P.SPATIAL_STATIC, P.SPATIAL_OFF],
        labels={P.SPATIAL_DYNAMIC: "Dinámico", P.SPATIAL_STATIC: "Estático", P.SPATIAL_OFF: "Off"},
        caption={}),
    "scene": dict(
        values=[P.SCENE_MUSIC, P.SCENE_EXERCISE, P.SCENE_MOVIE],
        labels={P.SCENE_MUSIC: "Música", P.SCENE_EXERCISE: "Ejercicio", P.SCENE_MOVIE: "Película"},
        caption={}),
}
SWITCHES = [("game", "Modo juego", "Baja latencia"),
            ("wind", "Ruido de viento", "Reduce el viento en llamadas"),
            ("multi", "Multipunto", "Conectar dos dispositivos a la vez"),
            ("auto", "Iniciar con Windows", None)]
W, H = 320, 650


def system_theme() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def reduce_motion() -> bool:
    on = ctypes.c_int(1)  # SPI_GETCLIENTAREAANIMATION
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)
    return not on.value


class Spring:
    """Resorte críticamente amortiguado (damping 1.0). Siempre parte del valor actual."""
    def __init__(self, x, response=0.28):
        self.x = self.t = float(x)
        self.v = 0.0
        self.w = 2 * math.pi / response

    def set(self, t, snap=False):
        self.t = float(t)
        if snap:
            self.x, self.v = self.t, 0.0

    def step(self, dt):
        dt = min(dt, 0.05)
        a = -self.w * self.w * (self.x - self.t) - 2 * self.w * self.v
        self.v += a * dt
        self.x += self.v * dt

    @property
    def moving(self):
        return abs(self.x - self.t) > 0.002 or abs(self.v) > 0.01


class App:
    def __init__(self, start_hidden: bool):
        self.hs = Headset()
        self.ui_q: "queue.Queue[str]" = queue.Queue()
        self.touched = {}
        self.auto = autostart_enabled()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.k = self.root.winfo_fpixels("1i") / 96.0
        self.root.geometry(f"{self.px(W)}x{self.px(H)}")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(app_icon_path())
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        fams = set(tkfont.families())
        self.fam = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
        self.fam_d = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
        self.fam_l = "Segoe UI Light"

        self.theme = system_theme()
        self.c = PALETTES[self.theme]
        self.cv = tk.Canvas(self.root, width=self.px(W), height=self.px(H), highlightthickness=0, bd=0)
        self.cv.pack()
        self.imgs = {}
        self.regions = []
        self.pressed = None
        self.sp = {"bar": Spring(0, 0.5), "auto": Spring(int(self.auto))}
        for key in SEGMENTS:
            self.sp["seg_" + key] = Spring(0)
        for key, _t, _s in SWITCHES:
            self.sp.setdefault(key, Spring(0))
        self.last_t = time.time()
        self.running = False
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Motion>", self.on_motion)
        self.root.update_idletasks()
        self.apply_theme()

        self.tray = pystray.Icon("HaylouS40", make_icon(None, None, False), APP_NAME, self._menu())
        self.tray.run_detached()
        self._last_icon = None
        self.sync(snap=True)
        if start_hidden:
            self.root.withdraw()
        self.root.after(500, self.tick)

    # --- utilidades de dibujo ---
    def px(self, v):
        return int(round(v * self.k))

    def shape(self, w, h, color, r=None):
        w, h = max(2, self.px(w)), max(2, self.px(h))
        key = (w, h, color, r)
        if key not in self.imgs:
            S = 4
            im = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
            rad = (h * S / 2) if r is None else self.px(r) * S
            ImageDraw.Draw(im).rounded_rectangle((0, 0, w * S - 1, h * S - 1), rad, fill=color)
            buf = io.BytesIO()
            im.resize((w, h), Image.LANCZOS).save(buf, "PNG")
            self.imgs[key] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        return self.imgs[key]

    def put(self, x, y, w, h, color, r=None):
        self.cv.create_image(self.px(x), self.px(y), image=self.shape(w, h, color, r), anchor="nw")

    def text(self, x, y, s, size, color, weight="normal", anchor="w", fam=None):
        self.cv.create_text(self.px(x), self.px(y), text=s, anchor=anchor, fill=color,
                            font=(fam or self.fam, -self.px(size), weight))

    def apply_theme(self):
        self.c = PALETTES[self.theme]
        self.imgs.clear()
        self.cv.configure(bg=self.c["bg"])
        self.root.configure(bg=self.c["bg"])
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dark = ctypes.c_int(1 if self.theme == "dark" else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
            col = self.c["bg"].lstrip("#")
            bgr = ctypes.c_int(int(col[4:6] + col[2:4] + col[0:2], 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(bgr), 4)  # color de barra (Win11)
        except Exception:
            pass

    # --- estado -> objetivos de animación ---
    def _fresh(self, key):
        return time.time() - self.touched.get(key, 0) > 3  # no pisar lo que el usuario acaba de tocar

    def sync(self, snap=False):
        s = self.hs.state
        conn = s["connected"]
        for key, seg in SEGMENTS.items():
            if s[key] in seg["values"] and self._fresh(key):
                self.sp["seg_" + key].set(seg["values"].index(s[key]), snap)
        for key, _t, _s in SWITCHES:
            if key == "auto":
                self.sp["auto"].set(1 if self.auto else 0, snap)
            elif s[key] is not None and self._fresh(key):
                self.sp[key].set(1 if s[key] else 0, snap)
        self.sp["bar"].set(s["battery"] if (s["battery"] is not None and conn) else 0, snap)
        self.kick()

    def kick(self):
        if reduce_motion():
            for sp in self.sp.values():
                sp.x, sp.v = sp.t, 0.0
        if not self.running:
            self.running = True
            self.last_t = time.time()
            self._frame()

    def _frame(self):
        now = time.time()
        dt, self.last_t = now - self.last_t, now
        moving = False
        for sp in self.sp.values():
            if sp.moving:
                sp.step(dt)
                moving = sp.moving or moving
        self.draw()
        if moving:
            self.root.after(16, self._frame)
        else:
            for sp in self.sp.values():
                sp.x, sp.v = sp.t, 0.0
            self.running = False

    # --- dibujo ---
    def segmented(self, key, y0, h=36, active=True):
        """Selector segmentado con resaltado animado. Devuelve el título de la selección."""
        c, s = self.c, self.hs.state
        seg = SEGMENTS[key]
        conn = s["connected"] and active
        n = len(seg["values"])
        x0, w = 24, 272
        self.put(x0, y0, w, h, c["track"], r=11)
        sw = (w - 4) / n
        cur = s[key]
        if conn and cur in seg["values"]:
            self.put(x0 + 2 + sw * self.sp["seg_" + key].x, y0 + 2, sw, h - 4, c["thumb"], r=9)
        for i, v in enumerate(seg["values"]):
            sel = conn and cur == v
            col = c["text"] if sel else c["dim"]
            if not conn:
                col = c["off"]
            elif self.pressed == ("seg", key, v):
                col = c["text"]
            self.text(x0 + 2 + sw * (i + .5), y0 + h / 2, seg["labels"][v], 11.5, col,
                      "bold" if sel else "normal", anchor="center")
            if conn:
                self.regions.append((x0 + 2 + sw * i, y0, x0 + 2 + sw * (i + 1), y0 + h,
                                     ("seg", key, v), lambda k=key, v=v: self.set_value(k, v)))

    def draw(self):
        c, s, cv = self.c, self.hs.state, self.cv
        conn = s["connected"]
        cv.delete("all")
        self.regions = []
        dim = c["dim"]
        if len(self.imgs) > 400:  # la barra genera muchos anchos; acotamos el caché
            self.imgs.clear()

        # cabecera
        self.text(24, 32, "Haylou S40", 17, c["text"], "bold", fam=self.fam_d)
        self.put(24, 50, 7, 7, c["green"] if conn else c["orange"])
        self.text(38, 54, s["status"], 11.5, dim)

        # batería
        b = s["battery"] if conn else None
        num = f"{b}" if b is not None else "–"
        f = tkfont.Font(family=self.fam_l, size=-self.px(52))
        cv.create_text(self.px(22), self.px(96), text=num, anchor="w",
                       fill=c["text"] if b is not None else dim, font=f)
        if b is not None:
            self.text(24 + f.measure(num) / self.k + 4, 106, "%", 20, dim, fam=self.fam_l)
        self.text(296, 106, "batería", 11.5, dim, anchor="e")
        self.put(24, 132, 272, 6, c["track"] if self.theme == "light" else c["off"])
        wbar = 272 * self.sp["bar"].x / 100
        if wbar >= 6:
            t = self.sp["bar"].t
            col = c["red"] if t <= 15 else c["orange"] if t <= 30 else c["green"]
            self.put(24, 132, round(wbar), 6, col)

        # ruido
        self.text(24, 168, "Control de ruido", 12, dim, "bold")
        self.segmented("anc", 184)
        cap = SEGMENTS["anc"]["caption"].get(s["anc"], "") if conn else "Conectá los auriculares para controlarlos"
        self.text(24, 238, cap, 11.5, dim)

        # audio espacial
        self.text(24, 270, "Audio espacial", 12, dim, "bold")
        self.segmented("spatial", 286)
        self.segmented("scene", 330, active=s["spatial"] in (P.SPATIAL_DYNAMIC, P.SPATIAL_STATIC))

        # opciones
        cy, rh = 392, 50
        self.put(24, cy, 272, rh * len(SWITCHES), c["card"], r=14)
        for i, (key, title, sub) in enumerate(SWITCHES):
            y = cy + rh * i
            enabled = conn or key == "auto"
            pressed = self.pressed == ("row", key)
            if pressed:
                self.put(24, y, 272, rh, c["press"], r=14 if i in (0, len(SWITCHES) - 1) else 2)
            tcol = c["text"] if enabled else dim
            if sub:
                self.text(40, y + 18, title, 13.5, tcol)
                self.text(40, y + 35, sub, 11, dim)
            else:
                self.text(40, y + rh / 2, title, 13.5, tcol)
            v = self.sp[key].x
            sw_w, sw_h = 48, 28
            sx, sy = 296 - 16 - sw_w, y + (rh - sw_h) / 2
            self.put(sx, sy, sw_w, sw_h, self._mix(c["off"], c["green"], v) if enabled else c["off"])
            kw = 27 if (pressed and enabled) else 22
            kx = sx + 3 + (sw_w - 6 - kw) * max(0.0, min(1.0, v))
            self.put(kx, sy + 3, kw, 22, "#FFFFFF" if enabled else "#EDEDF0")
            if i:
                cv.create_line(self.px(40), self.px(y), self.px(280), self.px(y), fill=c["hair"])
            if enabled:
                self.regions.append((24, y, 296, y + rh, ("row", key), lambda k=key: self.toggle(k)))

        # pie
        fy = cy + rh * len(SWITCHES) + 28
        for label, x, anc, wd, fn in (("Reconectar", 24, "w", 70, lambda: self.hs.send("reconnect")),
                                      ("Salir", 296, "e", 34, self.quit)):
            self.text(x, fy, label, 12, c["accent"], anchor=anc)
            xs = x if anc == "w" else x - wd
            self.regions.append((xs - 6, fy - 14, xs + wd + 6, fy + 14, ("foot", label), fn))

    @staticmethod
    def _mix(a, b, t):
        t = max(0.0, min(1.0, t))
        pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))

    # --- entrada: feedback al pulsar, acción al soltar dentro del mismo elemento ---
    def hit(self, e):
        x, y = e.x / self.k, e.y / self.k
        for x0, y0, x1, y1, ident, fn in self.regions:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return ident, fn
        return None, None

    def on_press(self, e):
        self.pressed, _ = self.hit(e)
        self.draw()

    def on_release(self, e):
        ident, fn = self.hit(e)
        was, self.pressed = self.pressed, None
        if ident and ident == was:
            fn()
        self.draw()

    def on_motion(self, e):
        ident, _ = self.hit(e)
        self.cv.configure(cursor="hand2" if ident else "")

    # --- acciones ---
    def set_value(self, key, v):
        self.touched[key] = time.time()
        self.hs.state[key] = v
        self.hs.send(key, v)
        self.sp["seg_" + key].set(SEGMENTS[key]["values"].index(v))
        self.kick()

    def toggle(self, key):
        if key == "auto":
            self.auto = not self.auto
            try:
                set_autostart(self.auto)
            except Exception:
                self.auto = autostart_enabled()
            self.sp["auto"].set(1 if self.auto else 0)
            self.kick()
            return
        self.touched[key] = time.time()
        v = 0 if self.sp[key].t >= 0.5 else 1
        self.hs.state[key] = v
        self.hs.send(key, v)
        self.sp[key].set(v)
        self.kick()

    def _menu(self):
        def mode_item(m):
            return pystray.MenuItem(MODE_NAMES[m], lambda *_: self.ui_q.put(f"mode:{m}"),
                                    checked=lambda _i, m=m: self.hs.state.get("anc") == m, radio=True)
        return pystray.Menu(
            pystray.MenuItem("Abrir", lambda *_: self.ui_q.put("show"), default=True),
            pystray.Menu.SEPARATOR,
            *[mode_item(m) for m in SEGMENTS["anc"]["values"]],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda *_: self.ui_q.put("quit")))

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.sync()

    def quit(self):
        try:
            self.tray.stop()
        except Exception:
            pass
        self.hs.stop()
        self.root.destroy()

    # --- ciclo lento: sincroniza estado, tema e icono ---
    def tick(self):
        try:
            while True:
                cmd = self.ui_q.get_nowait()
                if cmd == "show":
                    self.show()
                elif cmd == "quit":
                    self.quit()
                    return
                elif cmd.startswith("mode:"):
                    self.set_value("anc", int(cmd[5:]))
        except queue.Empty:
            pass
        s = self.hs.state
        conn, b = s["connected"], s["battery"]
        key = (b if conn else None, s["anc"], conn)
        if key != self._last_icon:
            self._last_icon = key
            self.tray.icon = make_icon(b, s["anc"], conn)
            tip = APP_NAME
            if conn:
                tip += f" — {b}%" if b is not None else ""
                tip += f" · {MODE_NAMES[s['anc']]}" if s["anc"] in MODE_NAMES else ""
            else:
                tip += " — desconectado"
            self.tray.title = tip
        if self.root.state() != "withdrawn":
            th = system_theme()
            if th != self.theme:
                self.theme = th
                self.apply_theme()
            self.sync()
            if not self.running:
                self.draw()
        self.root.after(500, self.tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    try:  # nitidez en pantallas de alta densidad
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    if not single_instance():
        sys.exit(0)
    App(start_hidden="--bandeja" in sys.argv).run()
