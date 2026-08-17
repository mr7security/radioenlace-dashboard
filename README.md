# Dashboard de Radioenlaces (Mimosa)

Monitorizacion de **señal y calidad RF** de los enlaces punto a punto Mimosa.
Un unico servicio en Python (solo libreria estandar, sin dependencias) sondea los
radios, guarda historico en SQLite y sirve el dashboard web.

Enlaces de la primera etapa:

| Enlace | Radio consultado |
|---|---|
| SEDE - AZULMED | 172.16.177.4 |
| CENUSA - VENUX | 172.16.172.4 |
| SEDE - VENUX | 172.16.178.3 |

Basta con apuntar a **un extremo de cada enlace**: el radio ya reporta tambien los
datos del otro extremo.

---

## Contenido del repositorio

| Archivo | Para que sirve |
|---|---|
| `proxy.py` | El servicio: sondea los radios, guarda historico y sirve la web |
| `dashboard.html` | La interfaz. La sirve el propio proxy |
| `radios.json.example` | Plantilla de configuracion |
| `deploy.sh` | Instala / actualiza todo en un Ubuntu |
| `dashboard-radioenlaces.service` | Unidad systemd (arranque automatico) |
| `ejemplo_respuesta.json` | Captura real de un radio, para probar el parser sin red |
| `.gitignore` | Excluye `radios.json` y la base de datos |

`radios.json` (la configuracion real) y `radioenlaces.db` (el historico) **no se
suben al repositorio**: viven solo en el servidor.

---

## Subir a GitHub

```bash
cd <carpeta-con-estos-archivos>
git init
git add .
git commit -m "Dashboard de radioenlaces Mimosa - version inicial"
git branch -M main
git remote add origin https://github.com/<usuario>/<repo>.git
git push -u origin main
```

Antes del `git add`, comprueba que no se cuela nada que no toca:

```bash
git status --short          # no debe aparecer radios.json ni *.db
```

> **Haz el repositorio privado.** No hay credenciales en el codigo, pero
> `radios.json.example`, `ejemplo_respuesta.json` y este README contienen
> informacion de la red interna: IPs y gateway, nombres de sede, numeros de serie
> de los equipos y las coordenadas GPS del emplazamiento. Si necesitas que el repo
> sea publico, antes hay que sustituir esos datos por valores de ejemplo.

---

## Desplegar en Ubuntu

En el servidor:

```bash
# 1. Clonar
sudo apt-get install -y git
git clone https://github.com/<usuario>/<repo>.git dashboard-radioenlaces
cd dashboard-radioenlaces

# 2. Instalar
sudo bash deploy.sh
```

`deploy.sh` crea un usuario de servicio sin login, copia el codigo a
`/opt/dashboard-radioenlaces`, genera `radios.json` a partir del ejemplo, instala la
unidad systemd y arranca el servicio con **arranque automatico al encender el
servidor** (`enable`) y **reinicio automatico si se cae** (`Restart=always`).

Volver a lanzarlo mas adelante actualiza el codigo **sin perder** ni la configuracion
ni el historico.

### 3. Ajustar los radios

```bash
sudo nano /opt/dashboard-radioenlaces/radios.json
sudo systemctl restart dashboard-radioenlaces
```

### 4. Probar

```bash
# Lectura puntual de los tres radios, por consola
sudo -u dashboard-radio python3 /opt/dashboard-radioenlaces/proxy.py --once

# Log en directo
journalctl -u dashboard-radioenlaces -f

# Estado y arranque automatico
systemctl status dashboard-radioenlaces
systemctl is-enabled dashboard-radioenlaces      # debe decir: enabled
```

Y abrir en el navegador: `http://<ip-del-servidor>:8080`

Si hay cortafuegos activo:

```bash
sudo ufw allow 8080/tcp
```

> El servicio corre sin privilegios, asi que **el puerto debe ser 1024 o superior**.
> Si quisieras servirlo en el 80, lo limpio es dejarlo en 8080 y poner delante un
> nginx como proxy inverso.

### Actualizar despues de un cambio

```bash
cd ~/dashboard-radioenlaces
git pull
sudo bash deploy.sh
```

---

## Comprobaciones previas recomendadas

Los tres radios estan en subredes distintas (`.177`, `.172`, `.178`). Antes de nada,
confirma desde el servidor que llega a los tres:

```bash
for ip in 172.16.177.4 172.16.172.4 172.16.178.3; do
  echo -n "$ip -> "
  curl -sk --max-time 5 "https://$ip/cgi/dashboard.php" | head -c 60
  echo
done
```

Cada uno deberia devolver algo que empiece por `{"realtime":{...`. Si alguno
devuelve HTML o nada, ese radio necesita usuario/password en `radios.json`, o no hay
ruta hasta el.

---

## De donde salen los datos

Cada radio expone toda su telemetria en:

```
GET https://<ip-del-radio>/cgi/dashboard.php
```

Devuelve un JSON con `realtime.signalmeter`, `connection`, `device_info`,
`remote_info`, `mimo` y `gps`.

> **Seguridad:** ese mismo JSON incluye un bloque `config` con la passphrase WPA en
> claro, el hash de la contraseña de administrador y la community SNMP. El proxy
> **descarta ese bloque por completo**: nunca se guarda en la base de datos ni llega
> al navegador. Aun asi, conviene revisar quien tiene acceso a la web de gestion de
> los radios.

El proxy ignora los certificados autofirmados de los radios (por eso el navegador
avisa pero el servicio no).

---

## Que se muestra

- **RSSI por cadena** (las 4 cadenas MIMO) y RSSI combinado.
  Ojo: el radio reporta `rssi_average` como valor **combinado**, unos 6 dB por
  encima de la señal real por cadena. El dashboard muestra ambos y usa la señal por
  cadena para umbrales y SNR, que es la escala habitual en punto a punto.
- **SNR** calculado sobre el ruido medido.
- **EVM por cadena**, medio y peor cadena.
- **Señal recibida en el extremo remoto** (de `remote_info.chan_info`): permite ver
  si el enlace es asimetrico, es decir, si uno de los dos lados oye peor.
- Modulacion (MCS), streams, tasas PHY, PER, reintentos y CRC.
- Potencia Tx local y remota, con el recorte automatico (TPC backoff).
- Temperaturas de ambos equipos, Ethernet e interfaz activa.
- **Satelites GPS**: en TDMA el sincronismo depende del GPS; perder satelites es
  causa directa de caidas.
- Grafica historica de RSSI y SNR (1 h / 6 h / 24 h / 7 d), marcando en rojo los
  tramos sin enlace.

## Semaforo

| Nivel | Cuando |
|---|---|
| Critico | RSSI por cadena ≤ −70 dBm, SNR < 15 dB, o temperatura ≥ 75 °C |
| Degradado | RSSI ≤ −60 dBm, SNR < 25 dB, EVM peor que −20 dB, desequilibrio entre cadenas > 6 dB, PER > 2 %, menos de 4 satelites GPS, temperatura ≥ 65 °C, Ethernet degradada |
| Sin enlace | El radio no responde o reporta desconectado |

Los umbrales estan al principio de `evaluar_estado()` en `proxy.py`, faciles de ajustar.

---

## Pendiente

- Confirmar que los radios de CENUSA-VENUX y SEDE-VENUX usan el mismo formato de
  campos (si son otro modelo o firmware, puede cambiar algun nombre).
- Decidir si se integra con el dashboard de sensores del CPD o se queda aparte.
- Evaluar **SNMP** como alternativa al endpoint web: mas ligero y no expone
  credenciales, a cambio de menos campos.
- Alertas por correo cuando un enlace cae o se degrada.
- Grafica de trafico real: el radio la ofrece en `throughput`, pendiente de
  confirmar unidades.
