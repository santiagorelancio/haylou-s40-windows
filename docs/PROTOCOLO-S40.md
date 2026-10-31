# Protocolo BLE del Haylou S40

Notas de ingeniería inversa, verificadas contra un S40 real en Windows 11 con `bleak`. Todo lo marcado como **verificado** se comprobó escribiendo el valor y leyéndolo de vuelta del auricular, o capturando lo que manda el propio auricular o la app oficial.

## Canales BLE

| Servicio | Característica | Uso |
|---|---|---|
| `0000cf00` | `cf05` (write-without-response) | **Aquí se escriben los comandos** |
| `0000cf00` | `cf06` (notify) | Existe, pero en el S40 no se observó tráfico |
| `0000ae00` | `ae01` (write-without-response) | No responde a los comandos |
| `0000ae00` | `ae02` (notify) | **Aquí llegan las respuestas** y los avisos |

Diferencia clave con el S30 Pro: allí las respuestas llegan por `cf06`.

> Solo un cliente BLE puede tener la sesión de control. Si otro dispositivo (la app del celular) la tiene, el auricular publica únicamente el servicio genérico (`1800`) y el cliente de Windows no ve `cf05`/`ae02`. Un segundo intento sin la caché de servicios (`use_cached_services=False`) y, si hace falta, apagar/encender los auriculares lo resuelve. Con el multipunto activado, PC y celular pueden convivir.

## Tramas

```
aa bb cc | tipo | op | len_hi len_lo | sn | cuerpo | dd ee ff
```

`len = len(cuerpo) + 1` (el `sn`). Tipos: `0x80` comando de escritura, `0xC0` lectura; las respuestas llevan `0x00`.

| Acción | Trama |
|---|---|
| SET de un atributo | `aabbcc 80 08 0004 01 02 <attr> <valor> ddeeff` |
| GET batería | `aabbcc c0 02 0005 02 00000004 ddeeff` |
| GET estado completo | `aabbcc c0 09 0005 09 ffffffff ddeeff` |

Respuesta a SET: `aabbcc 00 08 0002 00 01 ddeeff` (se devuelve igual con cualquier atributo, aunque no tenga efecto).
Respuesta de batería: termina en `02 02 <porcentaje> ddeeff`.

### Estado completo (op `0x09`)

El cuerpo de la respuesta (desde el byte 9) es una lista TLV: `<largo> <ordinal> <datos de largo-1 bytes>`. Primero vienen campos de identificación más largos (p. ej. `07 00 <dirección de 6 bytes>`) y después los ajustes, que son campos de un byte: `02 <ordinal> <valor>`. Conviene recorrerlo como TLV: si la dirección del dispositivo contiene un `02`, buscar tripletas `02 xx yy` a ciegas se desalinea y puede perder un ajuste. Los usados:

| Ordinal (lectura) | Atributo (escritura) | Función | Valores |
|---|---|---|---|
| `0x09` | `0x04` | Control de ruido | `0` Off · `1` ANC · `2` Ambiente · `4` Adaptativo |
| `0x0b` | `0x05` | Modo juego | `0` / `1` |
| `0x0e` | `0x09` | Multipunto | `0` / `1` (cambia también el ordinal `0x11`) |
| `0x12` | `0x0a` | Audio espacial | `0` Dinámico · `1` Estático · `2` Off |
| `0x13` | `0x0b` | Escenario espacial | `0` Música · `1` Ejercicio · `2` Película |
| `0x14` | `0x0c` | Reducción de ruido de viento | `0` / `1` |

### Avisos espontáneos (op `0x0e`)

Al pulsar el botón físico, el auricular manda sin que se lo pidan `aabbcc c0 0e 0004 <sn> 02 08 <modo> ddeeff`: el cuerpo es TLV como el del estado completo, con el campo `0x08` = modo de ruido (mismos valores que la tabla). La app lo aplica al instante y después pide el estado completo para confirmarlo.

Cómo se verificó cada modo de ruido: con el botón físico el auricular manda `aabbccc00e00040a0208<modo>ddeeff` (Off, Ambiente, ANC, en ese orden de pulsación); el adaptativo no se activa desde el botón, pero se vio que la app oficial escribe el valor `4`. El valor `3` se acepta pero no hace nada audible.

## Probado sin efecto en el S40

- Atributos `0x03`, `0x06` (auto-play), `0x07` (anti-vazamiento), `0x0d` (detección de uso): se aceptan pero no cambian ningún campo del estado.
- Nivel de ANC (config `11`, `[1, nivel]` por el comando `80 f2`, como en el S30 Pro): lo acepta con «ok», pero no cambia ningún campo ni se percibe diferencia.
- EQ (atributo `0x02`): los valores leídos de vuelta no coinciden con los escritos de forma consistente; no se pudo mapear a nombres.
- LDAC (atributo `0x08`): no se probó; la app oficial avisa que reinicia los auriculares y Windows no usa LDAC.
