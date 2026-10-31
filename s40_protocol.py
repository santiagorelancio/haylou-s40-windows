"""Protocolo BLE del Haylou S40 (chipset BES). Funciones puras, sin Bluetooth.

Basado en el trabajo de revolutedigital/haylou-s30-pro-windows (MIT) y verificado
contra un S40 real: los comandos se escriben en la característica cf05 y las
respuestas llegan por ae02.
"""
HEADER = bytes([0xAA, 0xBB, 0xCC])
FOOTER = bytes([0xDD, 0xEE, 0xFF])

SVC_WRITE = "0000cf05-0000-1000-8000-00805f9b34fb"
SVC_NOTIFY = ("0000ae02-0000-1000-8000-00805f9b34fb", "0000cf06-0000-1000-8000-00805f9b34fb")

# Atributos que se escriben (SET) y el ordinal con el que vuelven en el estado (GET)
ATTR_ANC, ATTR_GAME, ATTR_WIND = 0x04, 0x05, 0x0C
ATTR_MULTI, ATTR_SPATIAL, ATTR_SCENE = 0x09, 0x0A, 0x0B

ORD_ANC, ORD_GAME, ORD_WIND = 0x09, 0x0B, 0x14
ORD_MULTI, ORD_SPATIAL, ORD_SCENE = 0x0E, 0x12, 0x13

# Modos de ruido, verificados contra la app oficial y el botón físico:
# 0 = Off, 1 = ANC, 2 = Sonido ambiente, 4 = ANC adaptativo (solo desde la app).
ANC_NORMAL, ANC_ANC, ANC_TRANSP, ANC_ADAPTIVE = 0, 1, 2, 4
# Audio espacial: 0, 1 y 2 (2 = apagado, verificado)
SPATIAL_DYNAMIC, SPATIAL_STATIC, SPATIAL_OFF = 0, 1, 2
# Escenario espacial (igual en dinámico y estático): 0 = Música, 1 = Ejercicio, 2 = Película
SCENE_MUSIC, SCENE_EXERCISE, SCENE_MOVIE = 0, 1, 2

# Aviso espontáneo (op 0x0e) que manda el auricular al pulsar el botón físico:
# aabbcc c0 0e 0004 <sn> 02 08 <modo> ddeeff, con <modo> en los mismos valores que ANC_*.
OP_EVENT = 0x0E
EVT_ANC = 0x08


def _frame(kind: int, op: int, body: bytes, sn: int) -> bytes:
    n = len(body) + 1
    return HEADER + bytes([kind, op, n >> 8, n & 0xFF, sn]) + body + FOOTER


def build_set(attr: int, value: int, sn: int = 1) -> bytes:
    return _frame(0x80, 0x08, bytes([0x02, attr, value]), sn)


def build_get_battery(sn: int = 0x02) -> bytes:
    return _frame(0xC0, 0x02, bytes([0, 0, 0, 4]), sn)


def build_get_all(sn: int = 0x09) -> bytes:
    return _frame(0xC0, 0x09, (0xFFFFFFFF).to_bytes(4, "big"), sn)


def is_valid(frame: bytes) -> bool:
    return len(frame) >= 11 and frame[:3] == HEADER and frame[-3:] == FOOTER


def parse_battery(frame: bytes):
    """Porcentaje de batería (1-100) si es respuesta de GET_BATTERY, si no None."""
    if is_valid(frame) and frame[4] == 0x02 and frame[-6:-4] == b"\x02\x02":
        pct = frame[-4]
        if 0 < pct <= 100:
            return pct
    return None


def _tlv(frame: bytes, start: int):
    """Campos de un byte de una lista TLV (<largo> <id> <datos>) hasta el pie.
    Devuelve (campos, encajó_exacto)."""
    out, i, end = {}, start, len(frame) - 3
    while i < end:
        n = frame[i]
        if n == 0 or i + 1 + n > end:
            break
        if n == 2:
            out[frame[i + 1]] = frame[i + 2]
        i += 1 + n
    return out, i == end


def parse_event(frame: bytes) -> dict:
    """Aviso espontáneo del auricular (op 0x0e) -> {id: valor}; {} si no lo es."""
    if not (is_valid(frame) and frame[4] == OP_EVENT):
        return {}
    out, ok = _tlv(frame, 8)
    return out if ok else {}


def parse_run_info(frame: bytes) -> dict:
    """Respuesta de GET_RUN_INFO -> {ordinal: valor}. Valor 0xff = no disponible.

    El cuerpo es una lista TLV: <largo> <ordinal> <datos (largo-1 bytes)>. Solo se
    devuelven los campos de un byte (largo 2), que son los ajustes. Recorrerlo como
    TLV evita confundir un 0x02 dentro de la dirección del dispositivo con el
    inicio de un campo. Si la trama no encaja como TLV, se usa la búsqueda de
    tripletas «02 ord valor» como respaldo."""
    if not (is_valid(frame) and frame[4] == 0x09):
        return {}
    out, ok = _tlv(frame, 9)
    if ok:
        return out
    out, i, end = {}, 9, len(frame) - 3
    while i + 3 <= end:
        if frame[i] == 0x02:
            out[frame[i + 1]] = frame[i + 2]
            i += 3
        else:
            i += 1
    return out
