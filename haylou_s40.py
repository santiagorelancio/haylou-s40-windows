"""Haylou S40 para Windows: batería y modo (ANC / Normal / Transparencia).

Ventana pequeña + icono en la bandeja. Hablamos por Bluetooth LE con los
auriculares mientras siguen conectados como audio normal.
"""
import asyncio
import ctypes
import functools
import json
import logging
import logging.handlers
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
SHOW_EVENT = "HaylouS40_Show"  # una 2.ª copia lo activa para traer al frente la ventana ya abierta

log = logging.getLogger("haylou")
log.addHandler(logging.NullHandler())

MODE_NAMES = {P.ANC_NORMAL: "Off", P.ANC_ANC: "Cancelación de ruido (ANC)", P.ANC_ADAPTIVE: "ANC adaptativo", P.ANC_TRANSP: "Sonido ambiente"}
MODE_COLOR = {P.ANC_NORMAL: "#8E8E93", P.ANC_ANC: "#0A84FF", P.ANC_ADAPTIVE: "#5E5CE6", P.ANC_TRANSP: "#30D158"}



# ───────────────────────── configuración ─────────────────────────
def setup_logging():
    """Registro en %APPDATA%\\HaylouS40\\haylou.log (rotativo): la app no tiene consola."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(APP_DIR, "haylou.log"),
                                                 maxBytes=200_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    except Exception:
        pass
    sys.excepthook = lambda *a: log.error("Excepción no controlada", exc_info=a)
    threading.excepthook = lambda a: log.error(
        "Excepción en hilo %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        log.exception("No se pudo leer la configuración")
        return {}


def save_config(cfg: dict):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(tmp, CONFIG_PATH)  # escritura atómica: nunca queda a medias
    except Exception:
        log.exception("No se pudo guardar la configuración")


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


def repair_autostart():
    """Si el inicio con Windows apunta a una ruta vieja (carpeta movida o reinstalada), lo corrige.
    Desde el código fuente solo se toca si el destino guardado ya no existe."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            stored = winreg.QueryValueEx(k, "HaylouS40")[0]
    except OSError:
        return
    target = stored.split('"')[1] if stored.startswith('"') and stored.count('"') >= 2 else stored.split(" ")[0]
    if stored != exe_command() and (getattr(sys, "frozen", False) or not os.path.exists(target)):
        try:
            set_autostart(True)
            log.info("Inicio con Windows actualizado: %s", exe_command())
        except Exception:
            log.exception("No se pudo actualizar el inicio con Windows")


def single_instance(signal_existing=True) -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "HaylouS40_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return True
    if signal_existing:  # avisa a la copia que ya corre para que muestre su ventana
        try:
            k32 = ctypes.windll.kernel32
            k32.OpenEventW.restype = ctypes.c_void_p
            h = k32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
            if h:
                k32.SetEvent(ctypes.c_void_p(h))
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass
    return False


# ───────────────────────── Bluetooth ─────────────────────────
SETTABLE = {"anc": P.ATTR_ANC, "game": P.ATTR_GAME, "wind": P.ATTR_WIND,
            "spatial": P.ATTR_SPATIAL, "multi": P.ATTR_MULTI, "scene": P.ATTR_SCENE}


# Cadencias de sondeo (s). Con la ventana oculta solo hace falta la batería del
# icono de bandeja: los cambios hechos con el botón físico llegan solos por notificación.
POLL_STATE_VISIBLE = 5
POLL_BATT_VISIBLE = 30
POLL_BATT_HIDDEN = 120
RETRY_MIN, RETRY_MAX_HIDDEN, RETRY_MAX_VISIBLE = 8, 60, 15


class Headset:
    """Hilo con su propio asyncio loop, totalmente dirigido por eventos: duerme hasta
    el próximo comando, aviso o sondeo programado (sin despertares periódicos)."""

    def __init__(self, on_change=None):
        self.cfg = load_config()
        self.state = {"connected": False, "status": "Buscando auriculares…",
                      "battery": None, "anc": None, "game": None, "wind": None,
                      "spatial": None, "multi": None, "scene": None}
        self.on_change = on_change  # se llama desde el hilo BLE; debe ser seguro entre hilos
        self.loop = asyncio.new_event_loop()
        self.aq: "asyncio.Queue[tuple]" = asyncio.Queue()
        self.client = None
        self.run = True
        self.active = False  # ventana visible
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    # --- API para la UI ---
    def send(self, kind, value=None):
        try:
            self.loop.call_soon_threadsafe(self.aq.put_nowait, (kind, value))
        except RuntimeError:
            pass

    def stop(self):
        self.run = False
        self.send("quit")

    # --- internos ---
    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.debug("on_change falló", exc_info=True)

    def _status(self, text, connected=None):
        self.state["status"] = text
        if connected is not None:
            self.state["connected"] = connected
        self._changed()

    def _on_notify(self, _, data):
        b = bytes(data)
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
            self.send("refresh")
        self._changed()

    def _on_disconnect(self, _client):
        self.send("lost")

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
            client = BleakClient(dev, timeout=15.0, disconnected_callback=self._on_disconnect,
                                 winrt={"use_cached_services": False} if fresh else {})
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
        log.info("Conectado a %s", dev.address)
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
            log.debug("Error al desconectar", exc_info=True)
        self.client = None
        self.state["connected"] = False

    def _retry_delay(self, fails):
        cap = RETRY_MAX_VISIBLE if self.active else RETRY_MAX_HIDDEN
        return min(RETRY_MIN * 1.5 ** max(0, fails - 1), cap)

    async def _main(self):
        inf = float("inf")
        fails = 0
        next_try = 0.0
        next_batt = next_state = inf
        while self.run:
            now = time.monotonic()
            if not self._alive():
                if self.state["connected"]:
                    log.info("Conexión perdida")
                    self._status("Conexión perdida, reconectando…", False)
                if now >= next_try:
                    try:
                        ok = await self._connect()
                        if ok:
                            await self._refresh()
                    except Exception as e:
                        ok = False
                        log.warning("No se pudo conectar: %r", e, exc_info=True)
                        await self._drop()
                        self._status(f"No se pudo conectar ({type(e).__name__}). Reintentando…", False)
                    now = time.monotonic()
                    if ok:
                        fails = 0
                        next_batt = now + (POLL_BATT_VISIBLE if self.active else POLL_BATT_HIDDEN)
                        next_state = now + POLL_STATE_VISIBLE if self.active else inf
                    else:
                        fails += 1
                        next_try = now + self._retry_delay(fails)
                deadline = next_try
            else:
                deadline = min(next_batt, next_state)
            try:
                kind, value = await asyncio.wait_for(self.aq.get(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                kind = value = None
            if kind == "quit":
                break
            now = time.monotonic()
            try:
                if kind == "reconnect":
                    await self._drop()
                    fails, next_try = 0, 0.0
                elif kind == "lost":
                    if not self._alive():
                        await self._drop()
                        self._changed()
                        next_try = now + 2
                elif kind == "active":
                    self.active = bool(value)
                    if self._alive():
                        if self.active:  # al abrir: datos frescos ya, y ritmo fluido
                            await self._refresh()
                            next_batt, next_state = now + POLL_BATT_VISIBLE, now + POLL_STATE_VISIBLE
                        else:
                            next_batt = min(next_batt, now + POLL_BATT_HIDDEN)
                            next_state = inf
                    elif self.active:  # reintenta ya al abrir la ventana si pasó un rato
                        fails, next_try = 0, min(next_try, now + 1)
                elif self._alive():
                    if kind in SETTABLE:
                        self.state[kind] = int(value)
                        await self._write(P.build_set(SETTABLE[kind], int(value)))
                    if kind in SETTABLE or kind == "refresh":
                        await asyncio.sleep(0.9)
                        await self._refresh(battery=False)
                        if self.active:
                            next_state = time.monotonic() + POLL_STATE_VISIBLE
                    elif kind is None:
                        batt_due = now >= next_batt
                        await self._refresh(battery=batt_due)
                        if batt_due:
                            next_batt = now + (POLL_BATT_VISIBLE if self.active else POLL_BATT_HIDDEN)
                        next_state = now + POLL_STATE_VISIBLE if self.active else inf
            except Exception:
                log.warning("Error en la conexión con los auriculares", exc_info=True)
                await self._drop()
                self._changed()
        await self._drop()


# ───────────────────────── icono de bandeja ─────────────────────────
@functools.lru_cache(maxsize=4)
def _icon_font(size):
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except Exception:
        return ImageFont.load_default()


def make_icon(battery, anc, connected=True) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = MODE_COLOR.get(anc, "#8A8F98") if connected else "#4B5058"
    d.ellipse((2, 2, 62, 62), fill=color)
    text = "?" if battery is None or not connected else str(battery)
    d.text((32, 33), text, fill="white", font=_icon_font(30 if len(text) < 3 else 24), anchor="mm")
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
TOP_BAR = 18  # alto extra para los botones de ventana


def system_theme() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "dark"


_rm_cache = (0.0, False)


def reduce_motion() -> bool:
    global _rm_cache
    now = time.monotonic()
    if now - _rm_cache[0] > 5:  # la consulta al sistema no hace falta en cada fotograma
        on = ctypes.c_int(1)  # SPI_GETCLIENTAREAANIMATION
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)
        _rm_cache = (now, not on.value)
    return _rm_cache[1]


def set_eco_mode(on: bool):
    """Oculta -> prioridad mínima y «modo eficiencia» (EcoQoS) de Windows; visible -> normal."""
    try:
        k32 = ctypes.windll.kernel32
        proc = k32.GetCurrentProcess()
        k32.SetPriorityClass(proc, 0x40 if on else 0x20)  # IDLE / NORMAL
        state = (ctypes.c_ulong * 3)(1, 1, 1 if on else 0)  # Version, ControlMask, StateMask
        k32.SetProcessInformation(proc, 4, ctypes.byref(state), ctypes.sizeof(state))  # ProcessPowerThrottling
    except Exception:
        pass


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
        self.ui_q: "queue.Queue[str]" = queue.Queue()
        self.wake_ok = False  # se activa cuando la ventana ya existe
        self.hs = Headset(on_change=self.wake)
        self._after = None
        self._sig = None
        self._theme_t = 0.0
        self._visible = not start_hidden
        self.touched = {}
        self.auto = autostart_enabled()
        self._warned = set()  # umbrales de batería baja ya avisados
        if self.auto:
            repair_autostart()

        self.root = tk.Tk()
        self.root.report_callback_exception = lambda *a: log.error("Error en la interfaz", exc_info=a)
        self.root.title(APP_NAME)
        self.k = self.root.winfo_fpixels("1i") / 96.0
        log.info("Iniciando: escala %.2f", self.k)
        try:
            self.root.iconbitmap(app_icon_path())
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.update_idletasks()
        self.chrome = self.frameless()  # barra de título propia (semáforo estilo Apple)
        self.dy = TOP_BAR if self.chrome else 0
        self.root.geometry(f"{self.px(W)}x{self.px(H + self.dy)}")
        self.root.resizable(False, False)
        log.info("Ventana creada (barra propia: %s)", self.chrome)
        if self.chrome:  # tras quitar la barra, fija el tamaño exacto (Tk aún cuenta la barra vieja)
            try:
                u = ctypes.windll.user32
                u.SetWindowPos(u.GetParent(self.root.winfo_id()), 0, 0, 0,
                               self.px(W), self.px(H + self.dy), 0x0002 | 0x0004)  # NOMOVE | NOZORDER
            except Exception:
                log.debug("No se pudo fijar el tamaño de la ventana", exc_info=True)

        fams = set(tkfont.families())
        self.fam = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
        self.fam_d = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
        self.fam_l = "Segoe UI Light"

        self.theme = system_theme()
        self.c = PALETTES[self.theme]
        self.cv = tk.Canvas(self.root, width=self.px(W), height=self.px(H + self.dy), highlightthickness=0, bd=0)
        self.cv.pack()
        self.imgs = {}
        self.regions = []
        self.cregions = []  # zonas de la barra superior (coordenadas de ventana)
        self.hover_chrome = False
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
        self.cv.bind("<Leave>", self.on_leave)
        self.root.bind("<<Wake>>", lambda _e: self.tick())
        self.root.bind("<Map>", lambda e: self.tick() if e.widget is self.root else None)
        self.f_big = tkfont.Font(family=self.fam_l, size=-self.px(52))
        self.root.update_idletasks()
        self.apply_theme()

        self.tray = pystray.Icon("HaylouS40", make_icon(None, None, False), APP_NAME, self._menu())
        self.tray.run_detached()
        self._last_icon = None
        self.sync(snap=True)
        if start_hidden:
            self.root.withdraw()
            set_eco_mode(True)
        else:
            self.draw()
            self.hs.send("active", True)
        self.wake_ok = True
        threading.Thread(target=self._listen_show, daemon=True).start()
        self._after = self.root.after(500, self.tick)

    def _listen_show(self):
        """Espera sin consumir a que una 2.ª copia pida mostrar la ventana."""
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateEventW.restype = ctypes.c_void_p
            ev = k32.CreateEventW(None, False, False, SHOW_EVENT)
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            while ev and k32.WaitForSingleObject(ev, 0xFFFFFFFF) == 0:
                self.ui_q.put("show")
                self.wake()
        except Exception:
            log.exception("Falló el aviso entre copias")

    def frameless(self) -> bool:
        """Quita la barra de título nativa (sigue siendo una ventana normal: barra de tareas,
        minimizar y Alt+Tab funcionan). Si algo falla se conserva la barra nativa."""
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id())
            style = u.GetWindowLongW(hwnd, -16)  # GWL_STYLE
            u.SetWindowLongW(hwnd, -16, style & ~0x00C00000)  # sin WS_CAPTION
            # SWP_NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
            u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
            return True
        except Exception:
            log.exception("No se pudo quitar la barra de título nativa")
            return False

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

    def put(self, x, y, w, h, color, r=None, dy=None):
        dy = self.dy if dy is None else dy
        self.cv.create_image(self.px(x), self.px(y + dy), image=self.shape(w, h, color, r), anchor="nw")

    def text(self, x, y, s, size, color, weight="normal", anchor="w", fam=None):
        self.cv.create_text(self.px(x), self.px(y + self.dy), text=s, anchor=anchor, fill=color,
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
            if self.chrome:
                round_ = ctypes.c_int(2)  # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(round_), 4)
                hair = self.c["hair"].lstrip("#")
                edge = ctypes.c_int(int(hair[4:6] + hair[2:4] + hair[0:2], 16))
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(edge), 4)  # borde
        except Exception:
            log.debug("DWM: atributos no disponibles", exc_info=True)

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
        if self.running or not any(sp.moving for sp in self.sp.values()):
            return
        if reduce_motion():
            for sp in self.sp.values():
                sp.x, sp.v = sp.t, 0.0
            self.draw()
            return
        self.running = True
        self.last_t = time.time()
        self._frame()

    def _frame(self):
        if not self._visible:
            self.running = False
            return
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
        f = self.f_big
        cv.create_text(self.px(22), self.px(96 + self.dy), text=num, anchor="w",
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
                cv.create_line(self.px(40), self.px(y + self.dy), self.px(280), self.px(y + self.dy), fill=c["hair"])
            if enabled:
                self.regions.append((24, y, 296, y + rh, ("row", key), lambda k=key: self.toggle(k)))

        # pie
        fy = cy + rh * len(SWITCHES) + 28
        for label, x, anc, wd, fn in (("Reconectar", 24, "w", 70, lambda: self.hs.send("reconnect")),
                                      ("Salir", 296, "e", 34, self.quit)):
            self.text(x, fy, label, 12, c["accent"], anchor=anc)
            xs = x if anc == "w" else x - wd
            self.regions.append((xs - 6, fy - 14, xs + wd + 6, fy + 14, ("foot", label), fn))
        self.draw_chrome()

    def draw_chrome(self):
        """Botones de ventana estilo Apple: cerrar (a la bandeja), minimizar y zoom (no aplica)."""
        self.cregions = []
        if not self.chrome:
            return
        c, cv = self.c, self.cv
        specs = (("close", "#FF5F57", "#E0443E", "#4D0000", self.hide),
                 ("min", "#FEBC2E", "#DEA123", "#985700", self.minimize),
                 ("zoom", None, None, None, None))  # la ventana no se redimensiona: queda gris
        for i, (name, fill, edge, glyph, fn) in enumerate(specs):
            x, y = 14 + i * 20, 11
            if fill is None:
                fill, edge = c["off"], c["hair"]
            self.put(x, y, 12, 12, edge, dy=0)
            self.put(x + 1, y + 1, 10, 10, fill, dy=0)
            if self.hover_chrome and glyph:
                w = max(1, int(round(1.4 * self.k)))
                if name == "close":
                    for a, b in (((3.6, 3.6), (8.4, 8.4)), ((8.4, 3.6), (3.6, 8.4))):
                        cv.create_line(self.px(x + a[0]), self.px(y + a[1]), self.px(x + b[0]),
                                       self.px(y + b[1]), fill=glyph, width=w)
                else:
                    cv.create_line(self.px(x + 3), self.px(y + 6), self.px(x + 9), self.px(y + 6),
                                   fill=glyph, width=w)
            if fn:
                self.cregions.append((x - 4, y - 5, x + 16, y + 17, ("chrome", name), fn))

    @staticmethod
    def _mix(a, b, t):
        t = max(0.0, min(1.0, t))
        pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))

    # --- entrada: feedback al pulsar, acción al soltar dentro del mismo elemento ---
    def hit(self, e):
        x, y = e.x / self.k, e.y / self.k
        for x0, y0, x1, y1, ident, fn in self.cregions:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return ident, fn
        y -= self.dy
        for x0, y0, x1, y1, ident, fn in self.regions:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return ident, fn
        return None, None

    def start_drag(self):
        """Mueve la ventana con el movimiento nativo de Windows (incluye ajuste a bordes)."""
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id())
            u.ReleaseCapture()
            u.SendMessageW(hwnd, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception:
            log.debug("No se pudo arrastrar la ventana", exc_info=True)

    def on_press(self, e):
        self.pressed, _ = self.hit(e)
        if self.pressed is None and self.chrome and e.y / self.k < self.dy + 44:
            self.start_drag()  # zona de título: arrastrar
            return
        self.draw()

    def on_release(self, e):
        ident, fn = self.hit(e)
        was, self.pressed = self.pressed, None
        if ident and ident == was:
            fn()
        if self.root.state() == "normal":
            self.draw()

    def _set_hover(self, h):
        if h != self.hover_chrome:
            self.hover_chrome = h
            if not self.running:
                self.draw()

    def on_motion(self, e):
        ident, _ = self.hit(e)
        self.cv.configure(cursor="hand2" if ident else "")
        self._set_hover(bool(ident and ident[0] == "chrome"))

    def on_leave(self, _e):
        self._set_hover(False)

    def minimize(self):
        try:
            self.root.iconify()
        except tk.TclError:
            self.hide()

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
            pystray.MenuItem("Avisar de batería baja", self._toggle_lowbatt,
                             checked=lambda _i: self.hs.cfg.get("low_batt_notify", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda *_: self.ui_q.put("quit")))

    def wake(self):
        """Despierta el ciclo de la UI desde otro hilo (BLE o bandeja)."""
        if not self.wake_ok:
            return
        try:
            self.root.event_generate("<<Wake>>", when="tail")
        except Exception:
            self.wake_ok = False  # sin soporte entre hilos: queda el sondeo periódico

    def _set_visible(self, vis):
        if vis != self._visible:
            self._visible = vis
            self.hs.send("active", vis)
            set_eco_mode(not vis)

    def _toggle_lowbatt(self, *_):
        self.hs.cfg["low_batt_notify"] = not self.hs.cfg.get("low_batt_notify", True)
        save_config(self.hs.cfg)

    def _check_battery(self, conn, b):
        """Notificación de Windows al bajar del 20 % y del 10 %; se rearma al pasar del 30 %."""
        if not conn or b is None:
            return
        if b >= 30:
            self._warned.clear()
            return
        if not self.hs.cfg.get("low_batt_notify", True):
            return
        lvl = 10 if b <= 10 else 20 if b <= 20 else None
        if lvl is None or lvl in self._warned:
            return
        self._warned.update(l for l in (10, 20) if l >= lvl)
        msg = (f"Batería muy baja: {b} %. Carga los auriculares pronto." if lvl == 10
               else f"Batería baja: {b} %. Conviene cargar los auriculares.")
        try:
            self.tray.notify(msg, APP_NAME)
        except Exception:
            log.debug("No se pudo mostrar la notificación", exc_info=True)

    def hide(self):
        self.root.withdraw()
        self._set_visible(False)
        self.hover_chrome = False
        self.running = False
        self.cv.delete("all")  # libera los gráficos mientras no se ven
        self.imgs.clear()
        self._sig = None

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._set_visible(True)
        self.sync()
        self.draw()

    def quit(self):
        try:
            self.tray.stop()
        except Exception:
            pass
        self.hs.stop()
        self.root.destroy()

    # --- ciclo lento y por eventos: comandos, estado, tema e icono ---
    def tick(self):
        if self._after is not None:
            self.root.after_cancel(self._after)
            self._after = None
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
        self._check_battery(conn, b)
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
        visible = self.root.state() == "normal"
        self._set_visible(visible)
        if visible:
            now = time.monotonic()
            if now - self._theme_t > 2:
                self._theme_t = now
                th = system_theme()
                if th != self.theme:
                    self.theme = th
                    self.apply_theme()
                    self._sig = None
            self.sync()
            sig = (tuple(sorted(s.items())), self.auto, self.theme)
            if sig != self._sig and not self.running:  # solo redibuja si algo cambió
                self._sig = sig
                self.draw()
        # oculta: casi nunca hace falta despertar; los eventos llegan por <<Wake>>
        delay = 500 if visible else (2000 if self.wake_ok else 500)
        self._after = self.root.after(delay, self.tick)

    def run(self):
        log.info("Interfaz lista")
        self.root.mainloop()
        log.info("Cerrada")


if __name__ == "__main__":
    try:  # nitidez en pantallas de alta densidad
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    setup_logging()
    if not single_instance(signal_existing="--bandeja" not in sys.argv):
        log.info("Ya hay otra copia abierta; se cierra esta")
        sys.exit(0)
    log.info("Arranque %s", sys.argv)
    App(start_hidden="--bandeja" in sys.argv).run()
