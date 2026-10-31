import s40_protocol as p

# Frames reales capturados de un Haylou S40
BATT = bytes.fromhex("aabbcc000200050002020228ddeeff")
ALL = bytes.fromhex("aabbcc0009001d0009020901020a01020b00020c05020f00021202021300021400021500ddeeff")
ACK = bytes.fromhex("aabbcc000800020001ddeeff")

def check(name, got, exp):
    assert got == exp, f"{name}: {got!r} != {exp!r}"
    print("OK ", name)

check("bateria", p.parse_battery(BATT), 40)
check("bateria en otro frame", p.parse_battery(ALL), None)
ri = p.parse_run_info(ALL)
check("anc", ri[p.ORD_ANC], 1)
check("game", ri[p.ORD_GAME], 0)
check("wind", ri[p.ORD_WIND], 0)
FULL = bytes.fromhex("aabbcc0009005900090700c6bde531b22407010000000000000302f70009030100000000000000020400020501050600000000020700020800020900020a01020b00020c05020d00020e01020f00021000021100021202021300021400021500ddeeff")
rf = p.parse_run_info(FULL)
check("espacial off", rf[p.ORD_SPATIAL], 2)
check("multipunto on", rf[p.ORD_MULTI], 1)
check("sin campos fantasma", 0xF7 in rf, False)
# Dirección que termina en 0x02 justo antes de un campo: no debe tragarse el ANC
ADDR02 = bytes.fromhex("aabbcc0009000e00090700c6bde531b202020904ddeeff")
check("tlv con 02 en la dirección", p.parse_run_info(ADDR02), {p.ORD_ANC: p.ANC_ADAPTIVE})
# Trama que no encaja como TLV: respaldo con la búsqueda de tripletas
check("respaldo sin tlv", p.parse_run_info(bytes.fromhex("aabbcc000900050009ff020901ddeeff")), {p.ORD_ANC: 1})
check("ack no es run info", p.parse_run_info(ACK), {})
# Botón físico: aviso espontáneo con el modo nuevo (Off, Ambiente, ANC)
for modo in (p.ANC_NORMAL, p.ANC_TRANSP, p.ANC_ANC):
    ev = bytes.fromhex(f"aabbccc00e00040a0208{modo:02x}ddeeff")
    check(f"aviso botón modo {modo}", p.parse_event(ev), {p.EVT_ANC: modo})
check("run info no es aviso", p.parse_event(FULL), {})
check("aviso no es run info", p.parse_run_info(bytes.fromhex("aabbccc00e00040a020801ddeeff")), {})
check("set anc=1", p.build_set(p.ATTR_ANC, 1).hex(), "aabbcc8008000401020401ddeeff")
check("get battery", p.build_get_battery().hex(), "aabbccc00200050200000004ddeeff")
check("get all", p.build_get_all().hex(), "aabbccc009000509ffffffffddeeff")
print("TODO OK")
