#!/usr/bin/env python3
"""
Dashboard de Radioenlaces - servicio de datos
=============================================

Monitoriza radioenlaces de dos tecnologias y los normaliza a un formato comun:
  - Mimosa (serie B): leidos por HTTP/JSON desde /cgi/dashboard.php (con login).
  - Ceragon (FibeAir IP-20/IP-50): leidos por SNMP v2c.
Guarda un historico en SQLite y sirve el dashboard web (radioenlaces.html).

Solo usa libreria estandar de Python 3 (para Ceragon necesita las utilidades
'snmpget'/'snmpbulkwalk' del sistema: apt-get install snmp).

Uso:
    python3 radioenlaces.py                    # usa radios.json si existe
    python3 radioenlaces.py --once             # una lectura por consola (diagnostico)
    python3 radioenlaces.py --fixture f.json   # prueba el parser Mimosa con un JSON

NOTA DE SEGURIDAD: el endpoint Mimosa devuelve tambien credenciales (passphrase
WPA, hash de admin, community SNMP). Este servicio IGNORA por completo el bloque
'config': nunca se guarda en la base de datos ni se envia al navegador.
"""

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "radios.json")
DB_PATH = os.path.join(BASE_DIR, "radioenlaces.db")
HTML_PATH = os.path.join(BASE_DIR, "radioenlaces.html")

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

CONFIG_POR_DEFECTO = {
    "puerto": 8080,
    "intervalo_segundos": 15,
    "dias_historico": 30,
    "radios": [
        {"nombre": "SEDE - AZULMED", "host": "172.16.177.4", "usuario": "", "password": ""},
        {"nombre": "CENUSA - VENUX", "host": "172.16.172.4", "usuario": "", "password": ""},
        {"nombre": "SEDE - VENUX", "host": "172.16.178.3", "usuario": "", "password": ""},
    ],
}


def log(mensaje):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mensaje), flush=True)


def cargar_config():
    cfg = dict(CONFIG_POR_DEFECTO)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
            log("Configuracion cargada de %s" % CONFIG_PATH)
        except Exception as exc:  # noqa: BLE001
            log("ERROR leyendo %s (%s). Uso configuracion por defecto." % (CONFIG_PATH, exc))
    else:
        log("No existe %s; uso configuracion por defecto." % CONFIG_PATH)
    return cfg


# ---------------------------------------------------------------------------
# Cliente del radio (Mimosa)
# ---------------------------------------------------------------------------

CTX_SSL = ssl.create_default_context()
CTX_SSL.check_hostname = False
CTX_SSL.verify_mode = ssl.CERT_NONE  # los radios usan certificado autofirmado


class ClienteRadio:
    """Descarga /cgi/dashboard.php manteniendo la sesion (cookie) si hace falta."""

    # Usuario por defecto del firmware Mimosa (B24, B5c, C5x...). Se puede
    # sobreescribir por radio en radios.json con el campo "usuario".
    USUARIO_POR_DEFECTO = "configure"

    def __init__(self, host, usuario="", password="", timeout=8):
        self.host = host
        self.usuario = usuario or self.USUARIO_POR_DEFECTO
        self.password = password
        self.timeout = timeout
        self.opener = self._nuevo_opener()

    def _nuevo_opener(self):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(),  # tarro de cookies propio (PHPSESSID)
            urllib.request.HTTPSHandler(context=CTX_SSL),
        )
        op.addheaders = [("User-Agent", "dashboard-radioenlaces/1.0")]
        return op

    def _url(self, ruta):
        return "https://%s%s" % (self.host, ruta)

    def _get(self, ruta):
        with self.opener.open(self._url(ruta), timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def _login(self):
        """Reproduce el login de la interfaz web de Mimosa:
          1. GET /                      -> obtiene la cookie PHPSESSID
          2. POST /?q=index.login&mimosa_ajax=1  con username y password
        La sesion queda guardada en el tarro de cookies del opener.
        """
        if not self.password:
            return False
        # Sesion limpia: descartamos cualquier cookie caducada anterior
        self.opener = self._nuevo_opener()
        # 1. Cookie de sesion
        try:
            self._get("/")
        except Exception:  # noqa: BLE001
            pass
        # 2. Autenticacion
        datos = urllib.parse.urlencode(
            {"username": self.usuario, "password": self.password}
        ).encode()
        req = urllib.request.Request(
            self._url("/?q=index.login&mimosa_ajax=1"),
            data=datos,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            cuerpo = resp.read().decode("utf-8", "replace")
        # La respuesta trae "role": 1/2 si autentica, 0 si la clave es incorrecta
        try:
            rol = json.loads(cuerpo).get("role", 0)
        except Exception:  # noqa: BLE001
            rol = 0
        if not rol:
            raise RuntimeError("login rechazado (usuario o contrasena incorrectos)")
        return True

    @staticmethod
    def _es_payload(obj):
        """El JSON bueno trae 'realtime'. Sin sesion, el radio devuelve {'role':0}."""
        return isinstance(obj, dict) and "realtime" in obj

    def leer(self):
        """Devuelve el dict JSON crudo del radio, autenticando si hace falta."""
        try:
            obj = json.loads(self._get("/cgi/dashboard.php"))
        except Exception:  # noqa: BLE001
            obj = None
        if not self._es_payload(obj):
            # Sesion inexistente o caducada: autenticamos y reintentamos una vez
            if not self.password:
                raise RuntimeError("radio sin contrasena configurada en radios.json")
            self._login()
            obj = json.loads(self._get("/cgi/dashboard.php"))
        if not self._es_payload(obj):
            raise RuntimeError(
                "El radio no devolvio datos (revisa usuario/password en radios.json)"
            )
        return obj


# ---------------------------------------------------------------------------
# Cliente Ceragon (SNMP)
# ---------------------------------------------------------------------------
#
# Los Ceragon (FibeAir IP-50/IP-20) no dan JSON: se leen por SNMP v2c, que
# tienen habilitado. La telemetria de radio vive en la MIB propietaria 2281,
# indexada por "carrier" (cada equipo tiene 1 o 2 portadoras). Los OID de
# abajo estan confirmados contra un IP-50C real; si algun modelo difiere, se
# ajustan aqui sin tocar el resto del codigo.

CERAGON_BASE = "1.3.6.1.4.1.2281.10"

# Columna cuyos VALORES son los indices de portadora (p.ej. 268451905/906)
CERAGON_COL_INDICES = CERAGON_BASE + ".5.1.1.1"

# Columnas por portadora: se consultan como <columna>.<indice>
CERAGON_COLUMNAS = {
    "rsl": CERAGON_BASE + ".5.1.1.2",        # nivel de senal recibida (dBm)
    "tsl": CERAGON_BASE + ".5.1.1.3",        # potencia transmitida (dBm)
    "temp": CERAGON_BASE + ".5.1.1.5",       # temperatura de la radio (C, string)
    "serie": CERAGON_BASE + ".5.1.1.13",     # numero de serie
    "mse": CERAGON_BASE + ".7.1.1.2",        # MSE (calidad); viene x100 en dB
    "freq_rx": CERAGON_BASE + ".7.3.1.1.6",  # frecuencia Rx (kHz)
    "freq_tx": CERAGON_BASE + ".7.3.1.1.7",  # frecuencia Tx (kHz)
    "ip_remota": CERAGON_BASE + ".7.3.1.1.3",  # IP del extremo remoto
    "modulacion": CERAGON_BASE + ".7.4.1.1.6",  # perfil ACM (p.ej. 4096)
}

# OID de sistema (una sola vez, sin indice de portadora)
CERAGON_SISTEMA = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",
    "sysName": "1.3.6.1.2.1.1.5.0",
    "sysUptime": "1.3.6.1.2.1.1.3.0",   # centisegundos
    "modelo": CERAGON_BASE + ".1.2.3.0",
}

# PM de Capacity/Throughput. La tabla esta indexada por [interfaz-de-grupo].[intervalo];
# usamos el intervalo 1 (ultimo cerrado), que es estable y cuadra con la GUI.
# Columnas confirmadas contra la GUI (cualificador 2):
CERAGON_PM = {
    "cap_pico":  CERAGON_BASE + ".6.3.4.3.1.1.7.2",  # Peak capacity (Mbps)
    "cap_media": CERAGON_BASE + ".6.3.4.3.1.1.8.2",  # Average capacity (Mbps)
    "tp_pico":   CERAGON_BASE + ".6.3.4.3.1.1.4.2",  # Peak throughput (Mbps)
    "tp_media":  CERAGON_BASE + ".6.3.4.3.1.1.5.2",  # Average throughput (Mbps)
}

_SNMP_DISPONIBLE = None


def snmp_disponible():
    global _SNMP_DISPONIBLE
    if _SNMP_DISPONIBLE is None:
        _SNMP_DISPONIBLE = bool(shutil.which("snmpget") and shutil.which("snmpbulkwalk"))
    return _SNMP_DISPONIBLE


class ClienteCeragon:
    """Lee un Ceragon por SNMP v2c usando las utilidades del sistema."""

    def __init__(self, host, community="public", timeout=6):
        self.host = host
        self.community = community or "public"
        self.timeout = timeout

    def _correr(self, binario, oids):
        if not snmp_disponible():
            raise RuntimeError(
                "faltan las utilidades SNMP en el servidor (instala: apt-get install snmp)"
            )
        cmd = [
            binario, "-v2c", "-c", self.community,
            "-t", str(self.timeout), "-r", "1", "-On", "-Ot", "-Oq",
            self.host,
        ] + list(oids)
        try:
            salida = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout * 2 + 4
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("SNMP sin respuesta (timeout)")
        if salida.returncode != 0 and not salida.stdout.strip():
            err = (salida.stderr or "").strip().splitlines()
            raise RuntimeError("SNMP error: " + (err[0] if err else "desconocido"))
        return salida.stdout

    def _get(self, oids):
        """snmpget de varios OID. Devuelve {oid: valor}."""
        # -Oq da 'OID valor' (sin '= TIPO:'), mas facil de partir
        salida = self._correr("snmpget", oids)
        res = {}
        for linea in salida.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            partes = linea.split(None, 1)
            if len(partes) == 2:
                res[partes[0].lstrip(".")] = partes[1].strip().strip('"')
        return res

    def _indices(self):
        """Lista de indices de portadora leyendo la columna de indices."""
        salida = self._correr("snmpbulkwalk", [CERAGON_COL_INDICES])
        indices = []
        for linea in salida.splitlines():
            partes = linea.strip().split(None, 1)
            if len(partes) == 2:
                # el valor ES el indice de portadora
                v = partes[1].strip()
                if v.isdigit():
                    indices.append(v)
        return indices

    def _pm_columna(self, base_oid):
        """Valor de una columna del PM en el ultimo intervalo cerrado (intervalo 1).

        Si hay varias interfaces toma la mayor (grupo agregado). None si falla.
        """
        try:
            salida = self._correr("snmpbulkwalk", [base_oid])
        except Exception:  # noqa: BLE001
            return None
        mejor = None
        for linea in salida.splitlines():
            partes = linea.strip().split(None, 1)
            if len(partes) != 2:
                continue
            oid = partes[0].lstrip(".")
            # el ultimo segmento es el intervalo; 1 = ultimo cerrado
            if oid.rsplit(".", 1)[-1] != "1":
                continue
            v = _f(partes[1])
            if v is not None and (mejor is None or v > mejor):
                mejor = v
        return mejor

    def _pm_capacidad(self):
        """Devuelve las 4 metricas de Capacity/Throughput (Mbps) del PM."""
        return {clave: self._pm_columna(oid) for clave, oid in CERAGON_PM.items()}

    def leer(self):
        indices = self._indices()
        if not indices:
            raise RuntimeError("no se encontraron portadoras (SNMP vacio o community erronea)")

        # Pedimos todas las columnas de todas las portadoras en un solo snmpget
        oids = list(CERAGON_SISTEMA.values())
        etiquetas = {}  # oid -> (clave, indice)  para reconstruir
        for clave, col in CERAGON_COLUMNAS.items():
            for idx in indices:
                oid = "%s.%s" % (col, idx)
                oids.append(oid)
                etiquetas[oid] = (clave, idx)
        valores = self._get(oids)

        portadoras = {idx: {} for idx in indices}
        for oid, val in valores.items():
            if oid in etiquetas:
                clave, idx = etiquetas[oid]
                portadoras[idx][clave] = val

        sistema = {}
        for clave, oid in CERAGON_SISTEMA.items():
            sistema[clave] = valores.get(oid.lstrip("."))

        return {
            "indices": indices,
            "portadoras": portadoras,
            "sistema": sistema,
            "pm": self._pm_capacidad(),
        }


# ---------------------------------------------------------------------------
# Utilidades de parseo
# ---------------------------------------------------------------------------


def _f(valor, defecto=None):
    """float tolerante: descarta vacios, texto, NaN e infinitos.

    Los Mimosa emiten 'nan' o 'inf' en get_phy cuando el enlace se degrada;
    si se colasen romperian tanto int() como el JSON que consume el navegador.
    """
    try:
        if valor is None or valor == "":
            return defecto
        v = float(valor)
    except (TypeError, ValueError):
        return defecto
    return v if math.isfinite(v) else defecto


def _i(valor, defecto=None):
    v = _f(valor)
    return defecto if v is None else int(v)


def _primer_entero(texto, defecto=None):
    """'24170 MHz' -> 24170 (sin concatenar otros numeros que vengan detras)."""
    m = re.search(r"-?\d+", str(texto or ""))
    return int(m.group(0)) if m else defecto


def _media(valores):
    v = [x for x in valores if x is not None]
    return sum(v) / len(v) if v else None


def _redondear(valor, decimales=1):
    return None if valor is None else round(valor, decimales)


def parsear_get_phy(cadena):
    """El campo get_phy viene como querystring: rssi_0=-49.4&evm_0=-27.7&..."""
    resultado = {}
    if not cadena:
        return resultado
    for par in str(cadena).split("&"):
        if "=" not in par:
            continue
        clave, _, valor = par.partition("=")
        resultado[clave.strip()] = urllib.parse.unquote(valor.strip())
    return resultado


def parsear_chan_info(texto):
    """Campo del extremo remoto:
    '24090_24170_240_004_004_-048_-049_-049_-048_-022'
      freq1_freq2_ancho_pot1_pot2_rssi0_rssi1_rssi2_rssi3_evm
    Es la unica via para ver la senal que recibe el OTRO extremo del enlace.
    """
    partes = str(texto or "").split("_")
    if len(partes) < 10:
        return {}
    return {
        "rssi_cadenas": [_f(p) for p in partes[5:9]],
        "evm": _f(partes[9]),
        "tx_power": _f(partes[3]),
    }


def contar_satelites(texto):
    """'5,44:6,37:7,44' -> 3 satelites."""
    texto = str(texto or "").strip()
    return len([p for p in texto.split(":") if p]) if texto else None


def parsear_throughput_mimosa(texto):
    """Campo throughput del Mimosa: 'ts:tx,rx;ts:tx,rx;...' (kbps).
    Devuelve la ultima muestra (tx_mbps, rx_mbps)."""
    if not texto:
        return (None, None)
    # Ultima muestra no vacia (algunos radios dejan un ';' al final)
    segmentos = [s for s in str(texto).strip().split(";") if ":" in s]
    if not segmentos:
        return (None, None)
    ultima = segmentos[-1]
    _, _, par = ultima.partition(":")
    partes = par.split(",")
    tx = _f(partes[0]) if len(partes) > 0 else None
    rx = _f(partes[1]) if len(partes) > 1 else None
    # El radio reporta en kbps -> pasamos a Mbps
    a_mbps = lambda v: round(v / 1000.0, 1) if v is not None else None
    return (a_mbps(tx), a_mbps(rx))


def corregir_tput(tx, rx, tx_phy, rx_phy):
    """El throughput no puede superar la capacidad PHY. Si lo hace, el radio
    reporta el campo en una unidad mas fina (bps en vez de kbps, como algunos
    B01): reescalamos dividiendo otra vez por 1000. Autocorrige por modelo."""
    phy_max = max([p for p in (tx_phy, rx_phy) if p is not None], default=None)
    peor = max([v for v in (tx, rx) if v is not None], default=None)
    if phy_max and peor and peor > phy_max * 1.05:
        tx = round(tx / 1000.0, 1) if tx is not None else None
        rx = round(rx / 1000.0, 1) if rx is not None else None
    return (tx, rx)


def normalizar_distancia(valor):
    """Devuelve la distancia en metros.

    Los B24 la reportan en metros (p.ej. 689), pero otros modelos como el B01
    la dan en milimetros (p.ej. 2.90564e6 = 2905 m). Ningun radioenlace real
    supera los 100 km, asi que un valor mayor solo puede venir en mm.
    """
    d = _f(valor)
    if d is None:
        return None
    if d > 100000:  # > 100 km: imposible, viene en mm
        return round(d / 1000.0, 1)
    return round(d, 1)


# ---------------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------------


def parsear_radio(nombre, host, crudo):
    """Convierte la respuesta del Mimosa en un dict plano con lo que nos importa.

    Deliberadamente NO se toca crudo['config'] (contiene credenciales).
    """
    rt = crudo.get("realtime", {}) or {}
    sm = rt.get("signalmeter", {}) or {}
    con = rt.get("connection", {}) or {}
    dev = rt.get("device_info", {}) or {}
    rem = rt.get("remote_info", {}) or {}
    mimo = rt.get("mimo", {}) or {}
    gps = rt.get("gps", {}) or {}
    tp = rt.get("throughput", {}) or {}
    phy = parsear_get_phy(sm.get("get_phy", ""))
    remoto_rf = parsear_chan_info(rem.get("chan_info"))
    tput_tx, tput_rx = parsear_throughput_mimosa(tp.get("throughput"))
    tput_tx, tput_rx = corregir_tput(
        tput_tx, tput_rx, _f(phy.get("tx_phy_rate")), _f(phy.get("rx_phy_rate"))
    )

    rssi = [_f(phy.get("rssi_%d" % i)) for i in range(4)]
    evm = [_f(phy.get("evm_%d" % i)) for i in range(4)]
    tx_chain = [_f(mimo.get("tx_powerperchain%d" % i)) for i in range(4)]

    ruido1 = _f(sm.get("noise"))
    ruido2 = _f(sm.get("noise2"))
    ruido_medio = _media([ruido1, ruido2])

    # Ojo con las dos escalas de RSSI:
    #  - rssi_average es el valor COMBINADO de las 4 cadenas (~+6 dB)
    #  - la media por cadena es la referencia habitual para umbrales y SNR
    rssi_combinado = _f(phy.get("rssi_average"))
    rssi_cadena_media = _media(rssi)
    snr = None
    if rssi_cadena_media is not None and ruido_medio is not None:
        snr = round(rssi_cadena_media - ruido_medio, 1)

    evm_medio = _media(evm)
    evm_peor = max([e for e in evm if e is not None], default=None)

    conectado_raw = str(con.get("connected", "0")).strip().lower()

    return {
        "nombre": nombre,
        "host": host,
        "ts": int(time.time()),
        "tipo": "mimosa",
        # --- estado del enlace -------------------------------------------
        "conectado": conectado_raw in ("1", "true", "yes", "connected"),
        # 'enlace' es el nombre que le damos nosotros en radios.json;
        # 'enlace_equipo' es como esta bautizado dentro del propio radio.
        "enlace": nombre,
        "enlace_equipo": rem.get("friendlyname") or con.get("linkName") or "",
        "disponibilidad": _f(con.get("availability")),
        "uptime_s": _f(con.get("ConnectingUptime")),
        "distancia_m": normalizar_distancia(con.get("distance")),
        "rumbo": _f(con.get("heading")),
        # --- senal / calidad RF (extremo local) ---------------------------
        "rssi_combinado": rssi_combinado,
        "rssi_cadena_media": _redondear(rssi_cadena_media),
        "rssi_cadenas": rssi,
        "evm_cadenas": evm,
        "evm_medio": _redondear(evm_medio),
        "evm_peor": evm_peor,
        "ruido1": ruido1,
        "ruido2": ruido2,
        "ruido_medio": _redondear(ruido_medio),
        "snr": snr,
        "tx_power": _f(sm.get("totalPower")),
        "tx_power_cadenas": tx_chain,
        "freq1": _primer_entero(sm.get("Chains_1_2")),
        "freq2": _primer_entero(sm.get("Chains_3_4")),
        "ancho_canal": sm.get("BandWidth"),
        "banda": sm.get("OperatingBand"),
        # --- senal en el extremo remoto (via chan_info) -------------------
        "remoto_rf": {
            "rssi_cadenas": remoto_rf.get("rssi_cadenas", []),
            "rssi_medio": _redondear(_media(remoto_rf.get("rssi_cadenas", []))),
            "evm": remoto_rf.get("evm"),
            "tx_power": _f(sm.get("TotalPowerPeer")),
            "per": _f(rem.get("Remote_PER")),
            "tpc_backoff": _f(rem.get("tpc_backoff")),
        },
        # --- modulacion y capacidad --------------------------------------
        "tx_mcs": _i(phy.get("tx_mcs")),
        "rx_mcs": _i(phy.get("rx_mcs")),
        "tx_streams": _i(phy.get("tx_streams")),
        "rx_streams": _i(phy.get("rx_streams")),
        "tx_phy_mbps": _f(phy.get("tx_phy_rate")),
        "rx_phy_mbps": _f(phy.get("rx_phy_rate")),
        # Throughput real (Mbps) de la ultima muestra del radio
        "tput_tx_mbps": tput_tx,
        "tput_rx_mbps": tput_rx,
        "per_link": _f(phy.get("per_link")),
        "per_phy": _f(phy.get("per_phy")),
        "reintentos_tx": _i(phy.get("txretries"), 0),
        "fallos_tx": _i(phy.get("txfail"), 0),
        "crc_errores": _i(phy.get("cnt_mac_crc"), 0),
        # --- sincronismo GPS (critico en TDMA) ----------------------------
        "gps": {
            "satelites": contar_satelites(gps.get("Satellites")),
            "snr": _f(gps.get("SNR")),
            "precision": _f(gps.get("Precision")),
        },
        # --- equipos ------------------------------------------------------
        "local": {
            "nombre": dev.get("DeviceName"),
            "ip": dev.get("IPAddress"),
            "modelo": dev.get("Model"),
            "firmware": dev.get("Version"),
            "modo": dev.get("DeviceMode"),
            "temperatura": _f(dev.get("Temperature")),
            "ethernet": dev.get("ethernetSpeed"),
            "interfaz_activa": dev.get("activeNetworkInterface"),
            "eth_degradada": str(dev.get("ETHDownGraded", "0")) == "1",
            "ultimo_reboot": dev.get("LastReboot"),
            "motivo_reboot": dev.get("rebootReason"),
            "serie": dev.get("SerialNumber"),
        },
        "remoto": {
            "nombre": rem.get("DeviceName"),
            "ip": rem.get("IPAddress"),
            "modelo": rem.get("Model"),
            "firmware": rem.get("Version"),
            "modo": rem.get("DeviceMode"),
            "temperatura": _f(rem.get("Temperature")),
            "ethernet": rem.get("ethernetSpeed"),
            "interfaz_activa": rem.get("activeNetworkInterface"),
            "ultimo_reboot": rem.get("LastReboot"),
            "serie": rem.get("SerialNumber"),
        },
    }


def etiqueta_modulacion(valor):
    """El perfil ACM de Ceragon suele ser el orden QAM (4096, 2048, 1024...).
    Si es una potencia de 2 razonable lo mostramos como 'N QAM'; si no, crudo.
    Pendiente de confirmar contra la GUI de un Ceragon."""
    n = _i(valor)
    if n is None:
        return None
    if n in (2, 4):
        return "QPSK" if n == 4 else "BPSK"
    if 8 <= n <= 8192 and (n & (n - 1)) == 0:  # potencia de 2
        return "%d QAM" % n
    return "perfil %d" % n


def parsear_ceragon(nombre, host, datos):
    """Convierte la lectura SNMP de un Ceragon al mismo formato que los Mimosa.

    Un Ceragon tiene 1 o 2 portadoras; las tratamos como 'cadenas' para que el
    dashboard las pinte igual que las 4 cadenas MIMO de los Mimosa.
    """
    indices = datos.get("indices", [])
    portadoras = datos.get("portadoras", {})
    sistema = datos.get("sistema", {})

    rsl, mse, tsl, temps = [], [], [], []
    ip_remota = None
    freq_tx = freq_rx = None
    modulacion_raw = None
    serie = None
    for idx in indices:
        p = portadoras.get(idx, {})
        rsl.append(_f(p.get("rsl")))
        mse.append(_f(p.get("mse")))          # viene x100
        tsl.append(_f(p.get("tsl")))
        temps.append(_f(p.get("temp")))
        ip_remota = ip_remota or p.get("ip_remota")
        freq_tx = freq_tx or _f(p.get("freq_tx"))
        freq_rx = freq_rx or _f(p.get("freq_rx"))
        modulacion_raw = modulacion_raw or p.get("modulacion")
        serie = serie or p.get("serie")

    # MSE llega en centesimas de dB (-4303 -> -43.03)
    mse_db = [round(x / 100.0, 1) if x is not None else None for x in mse]

    rsl_medio = _media(rsl)
    mse_medio = _media(mse_db)
    mse_peor = max([x for x in mse_db if x is not None], default=None)

    # uptime en centisegundos -> segundos
    uptime = _f(sistema.get("sysUptime"))
    if uptime is not None:
        uptime = uptime / 100.0

    conectado = any(x is not None and x > -99 for x in rsl)
    temp_local = _media(temps)

    return {
        "nombre": nombre,
        "host": host,
        "ts": int(time.time()),
        "tipo": "ceragon",
        "conectado": conectado,
        "enlace": nombre,
        "enlace_equipo": sistema.get("sysName") or "",
        "disponibilidad": None,
        "uptime_s": uptime,
        "distancia_m": None,
        "rumbo": None,
        # RSL por portadora (tratado como 'cadenas' para el dashboard)
        "rssi_combinado": rsl_medio,
        "rssi_cadena_media": _redondear(rsl_medio),
        "rssi_cadenas": rsl,
        # MSE como equivalente al EVM (ambos negativos, mas bajo = mejor)
        "evm_cadenas": mse_db,
        "evm_medio": _redondear(mse_medio),
        "evm_peor": mse_peor,
        "ruido1": None,
        "ruido2": None,
        "ruido_medio": None,
        "snr": None,   # Ceragon no expone ruido/SNR por SNMP; usamos MSE
        "tx_power": _media(tsl),
        "tx_power_cadenas": tsl,
        "freq1": _i(freq_tx / 1000.0) if freq_tx else None,   # kHz -> MHz
        "freq2": _i(freq_rx / 1000.0) if freq_rx else None,
        "ancho_canal": None,
        "banda": None,
        "modulacion": etiqueta_modulacion(modulacion_raw),
        "pm": datos.get("pm") or {},   # cap_pico, cap_media, tp_pico, tp_media (Mbps)
        "throughput_mbps": (datos.get("pm") or {}).get("tp_media"),
        "remoto_rf": {},
        "tx_mcs": None, "rx_mcs": None, "tx_streams": None, "rx_streams": None,
        "tx_phy_mbps": None, "rx_phy_mbps": None,
        "per_link": None, "per_phy": None,
        "reintentos_tx": None, "fallos_tx": None, "crc_errores": None,
        "gps": {},
        "local": {
            "nombre": sistema.get("sysName") or nombre,
            "ip": host,
            "modelo": sistema.get("modelo"),
            "firmware": None,
            "modo": sistema.get("sysDescr"),
            "temperatura": _redondear(temp_local),
            "ethernet": None,
            "interfaz_activa": None,
            "eth_degradada": False,
            "ultimo_reboot": None,
            "motivo_reboot": None,
            "serie": serie,
        },
        "remoto": {
            "nombre": None,
            "ip": ip_remota,
            "modelo": sistema.get("modelo"),
            "firmware": None,
            "modo": None,
            "temperatura": None,
            "ethernet": None,
            "interfaz_activa": None,
            "ultimo_reboot": None,
            "serie": None,
        },
    }


def evaluar_estado(m):
    """Semaforo a partir de las metricas de RF. Umbrales referidos a la senal
    POR CADENA, que es la escala habitual en enlaces punto a punto."""
    if not m.get("conectado"):
        return "caido", ["Enlace desconectado"]

    avisos = []
    nivel = "ok"

    def subir(n):
        orden = {"ok": 0, "aviso": 1, "critico": 2}
        return n if orden[n] > orden[nivel] else nivel

    rssi = m.get("rssi_cadena_media")
    if rssi is not None:
        if rssi <= -70:
            nivel = subir("critico")
            avisos.append("RSSI por cadena muy bajo (%.1f dBm)" % rssi)
        elif rssi <= -60:
            nivel = subir("aviso")
            avisos.append("RSSI por cadena degradado (%.1f dBm)" % rssi)

    snr = m.get("snr")
    if snr is not None:
        if snr < 15:
            nivel = subir("critico")
            avisos.append("SNR bajo (%.1f dB)" % snr)
        elif snr < 25:
            nivel = subir("aviso")
            avisos.append("SNR justo (%.1f dB)" % snr)

    peor = m.get("evm_peor")
    if peor is not None and peor > -20:
        nivel = subir("aviso")
        avisos.append("EVM/MSE pobre en alguna portadora (%.1f dB)" % peor)

    # Desequilibrio entre cadenas/portadoras: sintoma de una cadena/antena tocada
    cadenas = [c for c in (m.get("rssi_cadenas") or []) if c is not None]
    if len(cadenas) >= 2 and (max(cadenas) - min(cadenas)) > 6:
        nivel = subir("aviso")
        avisos.append("Desequilibrio entre cadenas (%.1f dB)" % (max(cadenas) - min(cadenas)))

    for etiqueta, lado in (("local", "local"), ("remoto", "remoto")):
        t = (m.get(lado) or {}).get("temperatura")
        if t is not None and t >= 75:
            nivel = subir("critico")
            avisos.append("Temperatura %s alta (%.1f C)" % (etiqueta, t))
        elif t is not None and t >= 65:
            nivel = subir("aviso")
            avisos.append("Temperatura %s elevada (%.1f C)" % (etiqueta, t))

    per = m.get("per_link")
    if per is not None and per > 2:
        nivel = subir("aviso")
        avisos.append("PER elevado (%.1f%%)" % per)

    sat = (m.get("gps") or {}).get("satelites")
    if sat is not None and sat < 4:
        nivel = subir("aviso")
        avisos.append("Pocos satelites GPS (%d) - riesgo de perdida de sincronismo" % sat)

    if (m.get("local") or {}).get("eth_degradada"):
        nivel = subir("aviso")
        avisos.append("Ethernet local degradada")

    return nivel, avisos


# ---------------------------------------------------------------------------
# Historico
# ---------------------------------------------------------------------------

ESQUEMA = """
CREATE TABLE IF NOT EXISTS muestras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    radio TEXT NOT NULL,
    conectado INTEGER,
    rssi REAL, rssi0 REAL, rssi1 REAL, rssi2 REAL, rssi3 REAL,
    evm REAL, evm0 REAL, evm1 REAL, evm2 REAL, evm3 REAL,
    ruido REAL, snr REAL,
    rssi_remoto REAL, evm_remoto REAL,
    tx_mcs INTEGER, rx_mcs INTEGER,
    tx_phy REAL, rx_phy REAL,
    per REAL, temp_local REAL, temp_remoto REAL,
    tput_tx REAL, tput_rx REAL
);
CREATE INDEX IF NOT EXISTS idx_muestras_radio_ts ON muestras (radio, ts);
"""

# Columnas que se han ido añadiendo despues (para migrar BD antiguas con ALTER)
COLUMNAS_NUEVAS = {"tput_tx": "REAL", "tput_rx": "REAL"}

COLUMNAS = (
    "ts, radio, conectado, rssi, rssi0, rssi1, rssi2, rssi3,"
    " evm, evm0, evm1, evm2, evm3, ruido, snr, rssi_remoto, evm_remoto,"
    " tx_mcs, rx_mcs, tx_phy, rx_phy, per, temp_local, temp_remoto,"
    " tput_tx, tput_rx"
)
N_COLUMNAS = len(COLUMNAS.split(","))


class Historico:
    def __init__(self, ruta, dias=30):
        self.ruta = ruta
        self.dias = dias
        self.lock = threading.Lock()
        con = sqlite3.connect(self.ruta)
        try:
            con.executescript(ESQUEMA)
            # Migracion: añade columnas nuevas a bases de datos ya existentes
            for col, tipo in COLUMNAS_NUEVAS.items():
                try:
                    con.execute("ALTER TABLE muestras ADD COLUMN %s %s" % (col, tipo))
                except sqlite3.OperationalError:
                    pass  # ya existe
            con.commit()
        finally:
            con.close()

    def _con(self):
        con = sqlite3.connect(self.ruta, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def _ejecutar(self, sql, args=(), leer=False):
        with self.lock:
            con = self._con()
            try:
                cur = con.execute(sql, args)
                if leer:
                    return cur.fetchall()
                con.commit()
                return None
            finally:
                con.close()

    def guardar(self, m):
        rssi = list(m.get("rssi_cadenas") or []) + [None] * 4
        evm = list(m.get("evm_cadenas") or []) + [None] * 4
        remoto = m.get("remoto_rf") or {}
        fila = (
            m["ts"], m["nombre"], 1 if m.get("conectado") else 0,
            m.get("rssi_cadena_media"), rssi[0], rssi[1], rssi[2], rssi[3],
            m.get("evm_medio"), evm[0], evm[1], evm[2], evm[3],
            m.get("ruido_medio"), m.get("snr"),
            remoto.get("rssi_medio"), remoto.get("evm"),
            m.get("tx_mcs"), m.get("rx_mcs"),
            m.get("tx_phy_mbps"), m.get("rx_phy_mbps"),
            m.get("per_link"),
            (m.get("local") or {}).get("temperatura"),
            (m.get("remoto") or {}).get("temperatura"),
            m.get("tput_tx_mbps"), m.get("tput_rx_mbps"),
        )
        assert len(fila) == N_COLUMNAS, "columnas y valores descuadrados"
        marcadores = ",".join("?" * N_COLUMNAS)
        self._ejecutar(
            "INSERT INTO muestras (%s) VALUES (%s)" % (COLUMNAS, marcadores), fila
        )

    def purgar(self):
        limite = int(time.time()) - self.dias * 86400
        self._ejecutar("DELETE FROM muestras WHERE ts < ?", (limite,))

    def consultar(self, radio, horas=6, maximo=720):
        horas = min(max(horas, 0.1), 24 * 90)
        desde = int(time.time()) - int(horas * 3600)
        filas = self._ejecutar(
            "SELECT * FROM muestras WHERE radio = ? AND ts >= ? ORDER BY ts",
            (radio, desde),
            leer=True,
        )
        # Diezmado para no mandar miles de puntos al navegador
        if len(filas) > maximo:
            paso = len(filas) // maximo + 1
            filas = filas[::paso]
        return [dict(f) for f in filas]


# ---------------------------------------------------------------------------
# Sondeo
# ---------------------------------------------------------------------------


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.historico = Historico(DB_PATH, cfg.get("dias_historico", 30))
        self.clientes = {}
        self.hilos = {}
        self.estado = {}
        self.lock = threading.Lock()
        for r in cfg["radios"]:
            tipo = (r.get("tipo") or "mimosa").lower()
            if tipo == "ceragon":
                self.clientes[r["nombre"]] = ClienteCeragon(
                    r["host"], r.get("community", "public")
                )
            else:
                self.clientes[r["nombre"]] = ClienteRadio(
                    r["host"], r.get("usuario", ""), r.get("password", "")
                )

    def nombres(self):
        return [r["nombre"] for r in self.cfg["radios"]]

    def sondear_uno(self, r):
        nombre = r["nombre"]
        cliente = self.clientes[nombre]
        tipo = (r.get("tipo") or "mimosa").lower()
        try:
            crudo = cliente.leer()
            if tipo == "ceragon":
                m = parsear_ceragon(nombre, r["host"], crudo)
            else:
                m = parsear_radio(nombre, r["host"], crudo)
            m["nivel"], m["avisos"] = evaluar_estado(m)
            m["error"] = None
            self.historico.guardar(m)
        except Exception as exc:  # noqa: BLE001
            m = {
                "nombre": nombre,
                "host": r["host"],
                "ts": int(time.time()),
                "tipo": tipo,
                "conectado": False,
                "nivel": "caido",
                "avisos": [str(exc) or "Sin respuesta del radio"],
                "error": str(exc),
                "local": {},
                "remoto": {},
                "remoto_rf": {},
                "gps": {},
                "rssi_cadenas": [],
                "evm_cadenas": [],
            }
            log("%s (%s): %s" % (nombre, r["host"], exc))
        # Etiquetas de agrupacion (para tarjetas de enlace con dos lados)
        m["grupo"] = r.get("grupo") or nombre
        m["lado"] = r.get("lado") or ""
        m["color"] = r.get("color") or ""   # color propio de la tarjeta (opcional)
        with self.lock:
            self.estado[nombre] = m

    def ciclo(self):
        intervalo = max(5, int(self.cfg.get("intervalo_segundos", 15)))
        proxima_purga = time.time() + 3600
        while True:
            inicio = time.monotonic()
            for r in self.cfg["radios"]:
                anterior = self.hilos.get(r["nombre"])
                if anterior is not None and anterior.is_alive():
                    # El sondeo previo sigue en marcha: no lanzamos otro encima
                    log("%s: sondeo anterior aun en curso, salto este ciclo" % r["nombre"])
                    continue
                h = threading.Thread(target=self.sondear_uno, args=(r,), daemon=True)
                h.start()
                self.hilos[r["nombre"]] = h

            if time.time() >= proxima_purga:
                try:
                    self.historico.purgar()
                except Exception as exc:  # noqa: BLE001
                    log("Error purgando historico: %s" % exc)
                proxima_purga = time.time() + 3600

            espera = intervalo - (time.monotonic() - inicio)
            time.sleep(max(1, espera))

    def instantanea(self):
        with self.lock:
            estado = dict(self.estado)
        return {
            "actualizado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "intervalo": self.cfg.get("intervalo_segundos", 15),
            "radios": [estado[n] for n in self.nombres() if n in estado],
        }


# ---------------------------------------------------------------------------
# Servidor web
# ---------------------------------------------------------------------------


def json_seguro(obj):
    """allow_nan=False para que un NaN falle aqui y no rompa el navegador."""
    try:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        log("ERROR serializando JSON: %s" % exc)
        return json.dumps({"error": "datos no serializables"})


def crear_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DashboardRadioenlaces/1.0"

        def log_message(self, formato, *args):  # silenciar log de acceso
            pass

        def _responder(self, codigo, contenido, tipo="application/json; charset=utf-8"):
            datos = contenido if isinstance(contenido, bytes) else contenido.encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(datos)

        def do_GET(self):  # noqa: N802
            partes = urllib.parse.urlparse(self.path)
            ruta = partes.path
            params = urllib.parse.parse_qs(partes.query)

            if ruta in ("/", "/index.html", "/dashboard.html", "/radioenlaces.html"):
                try:
                    with open(HTML_PATH, "rb") as fh:
                        self._responder(200, fh.read(), "text/html; charset=utf-8")
                except FileNotFoundError:
                    self._responder(404, b"Falta radioenlaces.html", "text/plain; charset=utf-8")
                return

            if ruta == "/logo":
                # Logo de la empresa: primer archivo logo.* que exista junto al app
                for nombre in ("logo.png", "logo.svg", "logo.jpg", "logo.jpeg", "logo.gif"):
                    ruta_logo = os.path.join(BASE_DIR, nombre)
                    if os.path.exists(ruta_logo):
                        tipos = {
                            ".png": "image/png", ".svg": "image/svg+xml",
                            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                        }
                        ext = os.path.splitext(nombre)[1].lower()
                        with open(ruta_logo, "rb") as fh:
                            self._responder(200, fh.read(), tipos.get(ext, "image/png"))
                        return
                self._responder(404, b"sin logo", "text/plain; charset=utf-8")
                return

            if ruta == "/api/estado":
                self._responder(200, json_seguro(monitor.instantanea()))
                return

            if ruta == "/api/historico":
                radio = (params.get("radio") or [""])[0]
                if radio not in monitor.nombres():
                    self._responder(400, json_seguro({"error": "radio desconocido"}))
                    return
                horas = _f((params.get("horas") or ["6"])[0], 6)
                datos = monitor.historico.consultar(radio, horas)
                self._responder(200, json_seguro({"radio": radio, "datos": datos}))
                return

            self._responder(404, json_seguro({"error": "no encontrado"}))

    return Handler


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Dashboard de radioenlaces (Mimosa + Ceragon)")
    ap.add_argument("--once", action="store_true", help="una lectura y salir")
    ap.add_argument("--fixture", help="parsear un JSON Mimosa guardado en vez de consultar el radio")
    args = ap.parse_args()

    if args.fixture:
        with open(args.fixture, "r", encoding="utf-8") as fh:
            crudo = json.load(fh)
        m = parsear_radio("fixture", "-", crudo)
        m["nivel"], m["avisos"] = evaluar_estado(m)
        print(json.dumps(m, indent=2, ensure_ascii=False))
        return

    cfg = cargar_config()
    monitor = Monitor(cfg)

    if args.once:
        for r in cfg["radios"]:
            monitor.sondear_uno(r)
        print(json.dumps(monitor.instantanea(), indent=2, ensure_ascii=False))
        return

    threading.Thread(target=monitor.ciclo, daemon=True).start()

    puerto = cfg.get("puerto", 8080)
    servidor = ThreadingHTTPServer(("0.0.0.0", puerto), crear_handler(monitor))
    log("Dashboard de radioenlaces escuchando en http://0.0.0.0:%d" % puerto)
    log("Radios monitorizados: %s" % ", ".join(monitor.nombres()))
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log("Parando.")


if __name__ == "__main__":
    main()
