# Servicio SuperFoto UNO Q: iniciar, detener y arranque automático

`scripts/servicio.sh` es el envoltorio oficial para operar el servidor de
SuperFoto en la Arduino UNO Q **sin `sudo` y sin systemd**. Arranca el proceso
en segundo plano, lo vigila con un health check, evita instancias duplicadas y
permite dejarlo configurado para que suba solo después de cada reinicio de la
placa.

Este documento explica qué problema resuelve, cómo está construido por dentro,
cómo usarlo en el día a día y qué hacer cuando algo falla. Está pensado tanto
para quien instala el proyecto por primera vez como para quien ya lo tiene
corriendo y necesita reiniciarlo sin miedo.

> **Alcance:** Linux con `bash`, `python3` y OpenCV. La placa es una Arduino
> UNO Q con Debian 13 `aarch64`, usuario `arduino`, SSH en el puerto `2222` y
> el proyecto desplegado en `~/superfoto-unoq`. El script también funciona en
> una notebook Linux para desarrollo local.

---

## Índice

1. [Qué problema resuelve](#1-qué-problema-resuelve)
2. [Por qué sin sudo ni systemd](#2-por-qué-sin-sudo-ni-systemd)
3. [Arquitectura del proceso](#3-arquitectura-del-proceso)
4. [Antes de empezar: desplegar el proyecto](#4-antes-de-empezar-desplegar-el-proyecto)
5. [Comandos](#5-comandos)
6. [Variables de entorno](#6-variables-de-entorno)
7. [Arranque automático con cron](#7-arranque-automático-con-cron)
8. [Alternativa systemd](#8-alternativa-systemd)
9. [Operación diaria](#9-operación-diaria)
10. [Seguridad](#10-seguridad)
11. [Solución de problemas](#11-solución-de-problemas)
12. [Preguntas frecuentes](#12-preguntas-frecuentes)
13. [Referencias del repositorio](#13-referencias-del-repositorio)

---

## 1. Qué problema resuelve

Antes de este script, el servidor se arrancaba a mano:

```bash
python3 app/server.py --host 0.0.0.0 --port 8010
```

Eso funciona para una prueba, pero deja varios frentes abiertos:

- el proceso queda atado a la terminal; al cerrar SSH se pierde;
- no hay una forma simple de saber si sigue vivo;
- es fácil arrancarlo dos veces y chocar contra el puerto;
- `deploy_unoq.sh` ya levantaba el servidor con `nohup`, pero no dejaba una
  interfaz uniforme para detenerlo, consultarlo o reiniciarlo;
- no había arranque automático sin privilegios de administrador.

`scripts/servicio.sh` centraliza el ciclo de vida completo:

| Necesidad | Respuesta del script |
|---|---|
| Arrancar en segundo plano | `start` con `nohup`, PID file y log |
| Detener sin dejar huérfanos | `stop` con `TERM`, espera y `KILL` de último recurso |
| Saber si está vivo | `status` con PID, URL y `GET /api/health` |
| Ver qué pasó | `logs [N]` sobre `data/server.log` |
| Reiniciar tras un cambio | `restart` |
| Evitar duplicados | lock con `flock` + PID validado |
| No pisar otro servicio | detecta puerto ocupado y **no mata procesos ajenos** |
| Subir solo al reiniciar | `boot-enable` vía `crontab` del usuario |

El script es **idempotente**: `start` dos veces no duplica el proceso y `stop`
dos veces no falla.

---

## 2. Por qué sin sudo ni systemd

La UNO Q se usa con el usuario `arduino`, que no tiene `sudo` disponible. El
proyecto entero está diseñado con esa restricción: el despliegue es por SSH,
las dependencias vienen en la imagen de Debian y el servicio corre como usuario
común. Pedir `sudo` para operar el servicio rompería ese contrato.

La alternativa de systemd existe en el repositorio
([`systemd/superfoto-unoq.service`](../systemd/superfoto-unoq.service)), pero
instalarla implica copiar la unidad a `/etc/systemd/system/`, ejecutar
`daemon-reload` y `enable`, todo con `sudo`. Por eso queda documentada como
**opción avanzada**, no como camino principal.

Sin privilegios, el script usa herramientas que ya están en Debian y que
pertenecen al usuario:

| Pieza | Herramienta | Para qué |
|---|---|---|
| Segundo plano | `nohup` + `&` | el proceso sobrevive al cierre de SSH |
| Identidad | PID file + `/proc/<pid>/cmdline` | saber que el PID es realmente SuperFoto |
| Exclusión mutua | `flock` | evitar `start`/`stop` simultáneos |
| Puerto | `ss -ltn` | detectar si 8010 ya está ocupado |
| Salud | `curl` o `urllib` | consultar `/api/health` |
| Arranque | `crontab` del usuario | `@reboot` sin tocar el sistema |

**Ventajas:** no hay escalada de privilegios, no se modifica configuración del
sistema y todo el estado vive dentro de `data/`.

**Límites:** cron solo ejecuta al arrancar; no reinicia si el proceso se cae,
no rota logs con `logrotate` y no aplica límites de memoria. Para eso está la
unidad systemd opcional (ver [sección 8](#8-alternativa-systemd)).

---

## 3. Arquitectura del proceso

### 3.1 Vista general

```text
                    scripts/servicio.sh
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
          ▼                 ▼                 ▼
   data/servicio.lock  data/superfoto.pid  data/server.log
     (flock -n 9)       (PID validado)     (stdout+stderr)
          │                 │                 │
          └─────────────────┼─────────────────┘
                            ▼
              python3 app/server.py
              --host 0.0.0.0 --port 8010
                            │
                            ▼
                 HTTP :8010  /api/health
```

El servidor se lanza siempre desde la raíz del proyecto (`cd "$ROOT"`), con
`PYTHONUNBUFFERED=1` para que el log se escriba en tiempo real. El descriptor
del lock (`9`) se cierra en el hijo (`9>&-`): si lo heredara, el servidor
quedaría reteniendo el `flock` y un `stop` posterior fallaría con “otra
operación en curso”.

### 3.2 Archivos de estado

Todos viven en `data/` por defecto, o en el directorio que indique
`SUPERFOTO_DATA_DIR`:

| Archivo | Contenido | Se crea / borra |
|---|---|---|
| `data/superfoto.pid` | PID del servidor, una línea | `start` lo escribe; `stop` y `running_pid` lo borran si es inválido |
| `data/server.log` | `stdout` + `stderr` del servidor | `start` lo abre en modo append |
| `data/server.log.1` | log anterior tras rotar | aparece cuando `server.log` supera 5 MB |
| `data/servicio.lock` | archivo vacío usado por `flock` | se crea al primer comando; el lock se libera al terminar |

El PID file **no es una fuente de verdad ciega**. Antes de usarlo, el script
lee `/proc/<pid>/cmdline` y comprueba que la línea de comandos contenga
`app/server.py` (o la ruta absoluta del servidor). Si el PID fue reciclado por
otro programa, borra el archivo y sigue como si no estuviera corriendo.

### 3.3 Arranque (`start`)

1. Toma el lock exclusivo.
2. Si hay un PID válido, informa “ya está corriendo” y devuelve `0`.
3. Si el puerto está ocupado, intenta **adoptar** el proceso (ver 3.5).
4. Verifica que exista el intérprete, que exista `app/server.py` y que
   `python3` pueda importar `cv2` y `numpy`.
5. Crea `data/`, rota el log si hace falta y lanza:
   ```bash
   SUPERFOTO_DATA_DIR=... SUPERFOTO_PORT=8010 PYTHONUNBUFFERED=1 \
     nohup python3 app/server.py --host 0.0.0.0 --port 8010 \
     >> data/server.log 2>&1 &
   ```
6. Guarda `$!` en `data/superfoto.pid`.
7. Hace health check hasta 20 intentos cada 0,5 s (10 s en total). Si responde,
   informa `OK pid N - http://127.0.0.1:8010/`. Si no, muestra las últimas 30
   líneas del log y devuelve `1`.

### 3.4 Parada (`stop`)

1. Toma el lock.
2. Busca un PID válido en el PID file.
3. Si no hay, busca por `pgrep -f app/server.py` para cubrir el caso de un
   servidor arrancado por `deploy_unoq.sh` sin PID file.
4. Si el PID no parece de SuperFoto, **se niega a matarlo**.
5. Envía `TERM` y espera hasta 10 s (20 × 0,5 s). Si sigue vivo, envía `KILL`.
6. Borra el PID file e informa `detenido`.

### 3.5 Adopción de un proceso existente

`deploy_unoq.sh` reinicia el servicio por su cuenta con `nohup` y deja el PID
en `data/superfoto.pid`. Normalmente el script lo encuentra ahí. Pero también
puede pasar que el proceso esté vivo y el PID file se haya perdido, o que el
puerto esté ocupado por un servidor del mismo proyecto arrancado a mano.

En ese caso `start` no falla de inmediato: comprueba con `pgrep` si el dueño
del puerto es `app/server.py`. Si es así, escribe su PID en el PID file y
responde:

```text
servicio: ya esta corriendo (pid 4321, adoptado)
```

Si el puerto lo ocupa **cualquier otro programa**, no lo toca:

```text
servicio: el puerto 8010 esta ocupado por otro proceso; no lo toco.
Revisa: ss -ltnp | grep :8010
```

### 3.6 Health check

La verificación real es HTTP, no solo “el PID existe”:

```bash
curl -fsS --max-time 2 http://127.0.0.1:8010/api/health
```

- Se consulta a `127.0.0.1`, no a la IP de LAN: evita depender del firewall o
  de la interfaz de red.
- Si `curl` no está instalado, usa `urllib.request` de Python con timeout de
  2 s.
- El endpoint devuelve, entre otros campos, `ok`, `name`, `version`,
  `uptime_s`, `state`, `session_active`, `disk`, `python` y `opencv`.
- `status` usa el mismo chequeo para mostrar `health: OK` o `health: sin
  respuesta`.

### 3.7 Rotación del log

Antes de arrancar, si `data/server.log` supera **5 MB** (`5242880` bytes), se
renombra a `data/server.log.1` y se informa:

```text
servicio: log rotado a .../data/server.log.1
```

Solo se conserva una generación anterior. Si necesitás historial largo,
copiá `server.log.1` antes de un nuevo arranque o usá la unidad systemd con
`journald`.

### 3.8 Concurrencia

`flock -n 9` sobre `data/servicio.lock` garantiza que dos operaciones no se
pisen. Si ya hay una en curso:

```text
servicio: otra operacion de servicio esta en curso
```

El lock se libera automáticamente cuando termina el proceso que lo tomó. El
archivo `servicio.lock` puede quedar en disco; eso no indica que esté tomado.

### 3.9 El bug del lock y su corrección

Este punto fue un problema real durante el desarrollo. El servidor se lanza
dentro de un subshell que hereda el descriptor `9` usado por `flock`. Si el
hijo lo conserva abierto, el `flock` queda retenido mientras el servidor vive
y cualquier `stop` o `restart` posterior falla con:

```text
servicio: otra operacion de servicio esta en curso
```

La corrección está en la línea de arranque:

```bash
nohup "$PYTHON" "$SERVER" --host "$HOST" --port "$PORT" \
  >> "$LOG_FILE" 2>&1 9>&- &
```

`9>&-` cierra el descriptor en el proceso hijo. El servidor ya no retiene el
lock y `restart` funciona. **Verificado en la UNO Q:** `stop`, `start`,
`restart` y un nuevo ciclo `stop`/`start` completaron sin el error de lock.

### 3.10 Diagrama de estados

```text
                    scripts/servicio.sh start
        ┌──────────────────────────────────────────┐
        │                                          ▼
   ┌──────────┐                              ┌───────────┐
   │ DETENIDO │                              │ CORRIENDO │
   └──────────┘                              └───────────┘
        ▲                                          │
        │        scripts/servicio.sh stop          │
        └──────────────────────────────────────────┘

   start con proceso ya vivo  → queda CORRIENDO (idempotente)
   start con puerto ajeno    → sigue DETENIDO (no toca al intruso)
   caída del proceso         → queda DETENIDO (cron no reinicia)
   reinicio de la placa      → DETENIDO, salvo boot-enable
```

`status` devuelve `0` en `CORRIENDO` y `3` en `DETENIDO`. La transición
`CORRIENDO → DETENIDO` también ocurre si el proceso muere solo; en ese caso no
hay reinicio automático salvo que se use systemd.

---

## 4. Antes de empezar: desplegar el proyecto

El script no instala dependencias ni copia archivos: asume que el proyecto ya
está en la placa. El camino recomendado es
[`scripts/deploy_unoq.sh`](../scripts/deploy_unoq.sh), que usa SSH (sin
`rsync` y sin `sudo`):

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q> scripts/deploy_unoq.sh
```

Ese comando:

- empaqueta `app/`, `web/`, `scripts/`, `systemd/`, `docs/`,
  `config.example`, `requirements.txt` y `README.md`;
- preserva `data/sessions/`, previews y videos;
- copia por SSH al directorio remoto `~/superfoto-unoq`;
- da permisos de ejecución a `scripts/*.sh`;
- reinicia el servidor con `nohup` en el puerto `8010`;
- verifica `/api/health` durante 20 s y, si falla, muestra el log remoto.

Cuando termina, ya hay un servidor corriendo. A partir de ahí podés usar
`servicio.sh` para todas las operaciones:

```bash
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh status
```

Comprobaciones útiles antes de seguir:

```bash
# El script existe y es ejecutable
ls -l scripts/servicio.sh

# Python, OpenCV y NumPy disponibles
python3 -c 'import cv2, numpy; print(cv2.__version__)'

# El servidor responde
curl -s http://127.0.0.1:8010/api/health
```

Si `cv2` o `numpy` no están, `start` se detiene con un mensaje que menciona
`python3-opencv` y `python3-numpy`. En la imagen oficial de la UNO Q suelen
estar preinstalados; si faltan, hace falta intervención administrativa para
instalar paquetes del sistema.

> **Nota:** `deploy_unoq.sh` también acepta `SUPERFOTO_PORT` y rechaza
> explícitamente el puerto `8000` con un error. Usá siempre el mismo puerto en
> el despliegue y en `servicio.sh`.

---

## 5. Comandos

Estos son los comandos que se copian y pegan en la placa, dentro de
`~/superfoto-unoq`:

```bash
scripts/servicio.sh start
scripts/servicio.sh stop
scripts/servicio.sh restart
scripts/servicio.sh status
scripts/servicio.sh logs 50
scripts/servicio.sh boot-enable
scripts/servicio.sh boot-disable
scripts/servicio.sh boot-status
```

### 5.1 Resumen

| Comando | Qué hace | Código de salida |
|---|---|---|
| `start` | Arranca en segundo plano o adopta un proceso existente | `0` si corre; `1` si falla |
| `stop` | Detiene el servidor | `0` si quedó detenido; `1` si no puede |
| `restart` | `stop` + `start` | `0` si queda corriendo; `1` si falla |
| `status` | Muestra estado, PID, URL y health | `0` si corre; `3` si está detenido |
| `logs [N]` | Últimas `N` líneas del log (default 80) | `0`; `1` si no hay log |
| `boot-enable` | Agrega el `@reboot` al crontab del usuario | `0`; `1` si falla |
| `boot-disable` | Quita el `@reboot` | `0`; `1` si falla |
| `boot-status` | Indica si el arranque automático está activo | `0` habilitado; `1` deshabilitado |
| `help` | Muestra la ayuda | `0` |
| *(desconocido)* | Muestra la ayuda y termina | `2` |

El código `3` de `status` es deliberado: permite distinguir “detenido” de un
error real sin tener que parsear texto.

### 5.2 `start`

```bash
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh start
```

Primer arranque (salida típica; la ruta se muestra completa en la placa):

```text
servicio: iniciando en 0.0.0.0:8010 (datos: <ruta-del-proyecto>/data)
servicio: OK pid 4321 - http://127.0.0.1:8010/
```

Segundo arranque, sin duplicar:

```text
servicio: ya esta corriendo (pid 4321)
```

Proceso de `deploy_unoq.sh` adoptado:

```text
servicio: ya esta corriendo (pid 4321, adoptado)
```

Puerto ocupado por otro programa:

```text
servicio: el puerto 8010 esta ocupado por otro proceso; no lo toco.
Revisa: ss -ltnp | grep :8010
```

Fallo de salud al arrancar:

```text
servicio: ERROR: el servidor no respondio en 10 s. Ultimas lineas del log:
<...últimas 30 líneas de data/server.log...>
```

En ese caso el proceso puede haber quedado vivo o haber muerto; revisá
`status` y `logs 100` antes de reintentar.

### 5.3 `status`

```bash
scripts/servicio.sh status
```

Corriendo:

```text
estado:  corriendo
pid:     4321
url:     http://<IP_DE_LA_UNO_Q>:8010/
datos:   <ruta-del-proyecto>/data
log:     <ruta-del-proyecto>/data/server.log
health:  OK
{"ok": true, "name": "SuperFoto UNO Q", "version": "...", "uptime_s": 12.4, ...}
```

La IP que muestra `url` es la de la interfaz por la que sale el tráfico hacia
la LAN (se consulta la ruta por defecto con `ip -4 route get`); si no se puede
determinar, cae a `hostname -I` y finalmente a `127.0.0.1`. Esto evita mostrar
la bridge de Docker, que en la UNO Q aparecía primero con `hostname -I` y no es
la dirección que usa la notebook para entrar.

Detenido:

```text
estado:  detenido
datos:   <ruta-del-proyecto>/data
```

Y el comando termina con código `3`. Si además el puerto está ocupado por un
proceso ajeno, agrega:

```text
aviso:   el puerto 8010 esta ocupado por otro proceso
LISTEN 0 128 0.0.0.0:8010 0.0.0.0:* users:(("otro-programa",pid=999,fd=3))
```

`health: sin respuesta (revisa el log)` significa que el PID existe pero el
HTTP no contesta en 2 s. No siempre es fatal (puede estar procesando una
sesión pesada), pero conviene mirar el log.

### 5.4 `logs`

```bash
scripts/servicio.sh logs        # últimas 80 líneas
scripts/servicio.sh logs 300    # últimas 300
```

Si el log no existe:

```text
servicio: no hay log en <ruta-del-proyecto>/data/server.log
```

Para seguimiento en vivo:

```bash
tail -f data/server.log
```

Para buscar errores:

```bash
scripts/servicio.sh logs 500 | grep -i -E 'error|traceback|exception'
```

### 5.5 `stop`

```bash
scripts/servicio.sh stop
```

Salida normal:

```text
servicio: deteniendo pid 4321
servicio: detenido
```

Si el proceso no responde al `TERM` en 10 s:

```text
servicio: no termino en 10 s; enviando KILL
servicio: detenido
```

Si no estaba corriendo:

```text
servicio: no esta corriendo
```

Si el PID file apunta a un proceso que no es SuperFoto:

```text
servicio: el PID 999 no parece ser SuperFoto; no lo toco
```

### 5.6 `restart`

```bash
scripts/servicio.sh restart
```

Equivale a `stop` seguido de `start`. Si el servicio estaba detenido, lo
arranca. Es el comando habitual después de cambiar código o configuración que
no se aplicó con `deploy_unoq.sh`.

### 5.7 `boot-enable`, `boot-status`, `boot-disable`

```bash
scripts/servicio.sh boot-enable
scripts/servicio.sh boot-status
scripts/servicio.sh boot-disable
```

Salidas:

```text
servicio: arranque automatico habilitado:
@reboot <RUTA_DEL_PROYECTO>/scripts/servicio.sh start >/dev/null 2>&1 # superfoto-unoq-service
```

```text
arranque automatico: habilitado
@reboot <RUTA_DEL_PROYECTO>/scripts/servicio.sh start >/dev/null 2>&1 # superfoto-unoq-service
```

```text
arranque automatico: deshabilitado
```

`boot-enable` es idempotente: antes de agregar, filtra cualquier línea que
contenga la etiqueta `# superfoto-unoq-service`. Podés ejecutarlo varias veces
sin duplicar entradas. `boot-status` devuelve `0` si está habilitado y `1` si
no, lo que permite usarlo en scripts.

### 5.8 `help`

```bash
scripts/servicio.sh help
```

Imprime la lista de comandos, las variables de entorno, los archivos de estado
y la advertencia sobre el puerto 8000. También responde a `-h` y `--help`.

### 5.9 Verificación real en la UNO Q

El script fue probado en la placa con el servidor real. Resumen de la corrida
(los PID son efímeros y solo ilustran el orden de los eventos):

| Paso | Comando | Resultado observado |
|---|---|---|
| 1 | `scripts/servicio.sh stop` | servicio detenido; puerto 8010 libre |
| 2 | `scripts/servicio.sh start` | PID `12906`, health `OK` |
| 3 | `scripts/servicio.sh restart` | PID nuevo `13046`, health `OK`; sin error de lock |
| 4 | `scripts/servicio.sh stop` + `start` | PID final `13206` |
| 5 | `scripts/servicio.sh status` | `estado: corriendo`, URL y health correctos |
| 6 | `scripts/servicio.sh logs 6` | últimas 6 líneas del log |
| 7 | `scripts/servicio.sh boot-enable` (2 veces) | una sola línea en el crontab |
| 8 | `scripts/servicio.sh boot-status` | `arranque automatico: habilitado` |
| 9 | `curl` a `detector-tarjetas` en 8000 | intacto; el script nunca lo tocó |

La comprobación del puerto libre se hizo con `ss -ltn` y la del 8000 con una
consulta al otro servicio. **En la documentación no se incluye la IP real de la
placa**: usá siempre `<IP_DE_LA_UNO_Q>`.

### 5.10 Pruebas automatizadas del script

El script tiene pruebas propias en
[`tests/test_service_script.py`](../tests/test_service_script.py), sin arrancar
el servidor:

| Test | Qué comprueba |
|---|---|
| `test_script_existe_y_es_ejecutable` | archivo presente y bit `+x` |
| `test_sintaxis_bash` | `bash -n` sin errores |
| `test_comandos_y_guardas_de_seguridad` | comandos, PID, `flock`, health, crontab, `9>&-`, sin IPs hardcodeadas, mención del 8000 |
| `test_help` | ayuda con `Uso:`, `start`, `boot-enable` y `8000` |
| `test_status_con_data_dir_temporal` | `status` con `SUPERFOTO_DATA_DIR` temporal; código `0` o `3` |
| `test_rechaza_puerto_8000` | `SUPERFOTO_PORT=8000` termina con error, menciona `detector-tarjetas` y no crea el PID |

Se ejecutan con el resto de la suite:

```bash
python3 -m pytest -q tests/test_service_script.py
python3 -m pytest -q          # suite completa: 105 tests
```

---

## 6. Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `SUPERFOTO_PORT` | `8010` | Puerto HTTP. **Nunca 8000.** |
| `SUPERFOTO_HOST` | `0.0.0.0` | Interfaz de bind. `127.0.0.1` la limita a localhost. |
| `SUPERFOTO_DATA_DIR` | `<proyecto>/data` | Directorio de PID, log, lock y sesiones. |
| `PYTHON` | `python3` | Intérprete a usar. |

Ejemplos:

```bash
# Otro puerto (útil si 8010 está ocupado)
SUPERFOTO_PORT=8011 scripts/servicio.sh start
SUPERFOTO_PORT=8011 scripts/servicio.sh status

# Datos en otro disco o directorio
SUPERFOTO_DATA_DIR=~/superfoto-data scripts/servicio.sh start

# Intérprete explícito (por ejemplo, un venv)
PYTHON=/usr/bin/python3 scripts/servicio.sh status

# Solo accesible desde la propia placa
SUPERFOTO_HOST=127.0.0.1 scripts/servicio.sh restart
```

> **Importante:** el PID file no guarda el puerto ni el host. Si arrancás con
> `SUPERFOTO_PORT=8011`, usá la misma variable en `status`, `stop`, `logs` y
> `restart`. Si no, `status` puede mostrar una URL/health de 8010 aunque el
> proceso esté en 8011. Una opción cómoda es exportarla en la sesión:
> ```bash
> export SUPERFOTO_PORT=8011
> scripts/servicio.sh start
> scripts/servicio.sh status
> ```

### 6.1 El puerto 8000 está prohibido

En la placa convive `detector-tarjetas`, que usa el puerto **8000**. Este
proyecto usa **8010** y no debe tocarlo:

- `servicio.sh` **rechaza** `SUPERFOTO_PORT=8000` con error, antes de arrancar
  o detener nada; también valida que el valor sea numérico y esté en el rango
  1–65535;
- `deploy_unoq.sh` **también rechaza** `SUPERFOTO_PORT=8000` con error y código
  de salida `2`;
- si el 8010 está ocupado, elegí otro puerto libre (8011, 8020, etc.), nunca
  el 8000.

---

## 7. Arranque automático con cron

### 7.1 Qué hace `boot-enable`

Agrega una línea al **crontab del usuario** `arduino`:

```cron
@reboot /ruta/absoluta/al/proyecto/scripts/servicio.sh start >/dev/null 2>&1 # superfoto-unoq-service
```

- `@reboot` se ejecuta una vez, cuando arranca el servicio `cron`;
- no requiere `sudo`: `crontab` es del usuario y vive en su propio directorio;
- la etiqueta `# superfoto-unoq-service` permite identificar y quitar la línea
  sin tocar el resto del crontab;
- `start` es idempotente: si el servidor ya está corriendo (por ejemplo, porque
  un `deploy` lo levantó), no hace nada;
- la salida va a `/dev/null`; los mensajes del servidor van a
  `data/server.log`.

### 7.2 Verificar que cron está activo

```bash
systemctl is-active cron
# active

pgrep -x cron
# 512
```

Si `systemctl` no está disponible o no tenés permiso para consultarlo, probá:

```bash
service cron status
```

Sin cron activo, la línea `@reboot` nunca se ejecuta. En la placa oficial cron
está activo; si no lo está, hace falta que un administrador lo habilite.

### 7.3 Ver la línea instalada

```bash
crontab -l
```

Buscá la línea con la etiqueta `# superfoto-unoq-service`. `boot-status` hace
esa búsqueda por vos y devuelve un código de salida útil para scripts.

### 7.4 Probar el arranque automático

```bash
scripts/servicio.sh boot-enable
scripts/servicio.sh boot-status
sudo reboot   # si tenés permiso; si no, reiniciá la placa a mano
```

Después del reinicio, esperá unos segundos y comprobá:

```bash
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh status
scripts/servicio.sh logs 50
```

Si no arrancó, revisá en este orden: `boot-status`, `systemctl is-active cron`,
que la ruta del script exista y sea ejecutable, y el log del servidor.

### 7.5 Deshabilitar

```bash
scripts/servicio.sh boot-disable
scripts/servicio.sh boot-status   # arranque automatico: deshabilitado
```

`boot-disable` solo elimina la línea etiquetada; no toca otras tareas del
usuario.

### 7.6 Variables en el arranque automático

Cron no hereda las variables de tu sesión SSH. Si usás un puerto o un
directorio distinto, editá la línea del crontab (`crontab -e`) para incluirla:

```cron
@reboot SUPERFOTO_PORT=8011 SUPERFOTO_DATA_DIR=/ruta/a/los/datos /ruta/al/proyecto/scripts/servicio.sh start >/dev/null 2>&1 # superfoto-unoq-service
```

Si volvés a ejecutar `boot-enable`, la línea se reemplaza por la versión
estándar (sin variables). En ese caso, volvé a editarla o exportá las
variables en un wrapper propio.

---

## 8. Alternativa systemd

El repositorio incluye
[`systemd/superfoto-unoq.service`](../systemd/superfoto-unoq.service) como
**ejemplo**. No se instala por defecto porque requiere `sudo`, que en la placa
no está disponible.

### 8.1 Instalación (solo con acceso administrativo)

```bash
sudo cp systemd/superfoto-unoq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now superfoto-unoq
```

La unidad:

- corre como usuario `arduino`, no como root;
- usa `WorkingDirectory=~/superfoto-unoq` y `ExecStart` con la ruta absoluta;
- fija `SUPERFOTO_DATA_DIR` y `PYTHONUNBUFFERED=1`;
- tiene `Restart=on-failure` y `RestartSec=5`;
- limita la memoria a `1800M` y baja la prioridad con `Nice=5`.

### 8.2 Comparación con `servicio.sh`

| Aspecto | `scripts/servicio.sh` | systemd |
|---|---|---|
| Privilegios | usuario `arduino`, sin `sudo` | instalación con `sudo` |
| Arranque al boot | `cron @reboot` | `systemctl enable` |
| Reinicio si se cae | no | sí (`Restart=on-failure`) |
| Estado | `status` propio | `systemctl status` |
| Logs | `data/server.log` + rotación simple | `journalctl -u superfoto-unoq` |
| Límite de memoria | no | `MemoryMax=1800M` |
| Dependencia de red | no | `After=network-online.target` |
| Ideal para | operación diaria sin admin | instalación administrada |

**No mezcles ambos mecanismos** sobre el mismo puerto: si systemd ya tiene el
servicio y ejecutás `servicio.sh start`, el script adopta el proceso; un
`stop` lo terminaría y systemd podría reaccionar según su política. Elegí uno.

---

## 9. Operación diaria

### 9.1 Rutina después de un despliegue

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q> scripts/deploy_unoq.sh
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh status
curl -s http://127.0.0.1:8010/api/health | python3 -m json.tool
```

### 9.2 Reiniciar tras un cambio

```bash
scripts/servicio.sh restart
scripts/servicio.sh logs 80
```

### 9.3 Actualizar con `deploy_unoq.sh`

El despliegue normal detiene el servidor anterior, copia los archivos y
arranca uno nuevo. Al terminar, `servicio.sh status` debería verlo corriendo.
Si usaste `--no-restart`:

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q> scripts/deploy_unoq.sh --no-restart
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh restart
```

El despliegue preserva `data/sessions/`, previews y videos. Aun así, si vas a
hacer un cambio grande, conviene respaldar `data/` (ver
[`datasheet.md`](datasheet.md), sección de respaldo).

### 9.4 El puerto 8010 está ocupado

Diagnóstico:

```bash
scripts/servicio.sh status
ss -ltnp | grep :8010
pgrep -af 'app/server.py'
```

Casos:

- **Es nuestro servidor** (aparece `app/server.py`): `start` lo adopta o
  `status` lo muestra corriendo. No hagas nada más.
- **Es otro programa**: el script se niega a matarlo. Opciones:
  1. averiguar qué es y detenerlo por su propio mecanismo;
  2. usar otro puerto:
     ```bash
     export SUPERFOTO_PORT=8011
     scripts/servicio.sh start
     ```
     y desplegar con `SUPERFOTO_PORT=8011`;
  3. si el otro programa es `detector-tarjetas` (8000), no corresponde: ese
     servicio usa otro puerto y no debe tocarse.

### 9.5 El health no responde

```bash
scripts/servicio.sh status
scripts/servicio.sh logs 200
curl -v --max-time 3 http://127.0.0.1:8010/api/health
```

Si el PID existe pero el health falla:

1. mirá el log por excepciones o “disk full”;
2. comprobá espacio: `df -h .` y `du -sh data/sessions`;
3. si está procesando una sesión muy pesada, puede tardar; esperá y reintentá;
4. si sigue sin responder: `scripts/servicio.sh restart`;
5. si el reinicio falla, revisá `python3 -c 'import cv2, numpy'` y la
   existencia de `app/server.py`.

### 9.6 Leer el log

```bash
scripts/servicio.sh logs 200
tail -f data/server.log
ls -lh data/server.log data/server.log.1
```

El log mezcla `stdout` y `stderr`; el servidor usa `logging` y
`PYTHONUNBUFFERED=1`, así que las líneas aparecen en orden y sin buffering.

### 9.7 Una sola instancia por directorio de datos

El PID file y el lock protegen una instancia por `SUPERFOTO_DATA_DIR`. Si
arrancás dos servidores con **distinto** puerto pero el **mismo** `data/`,
ambos pueden escribir sesiones y previews en el mismo lugar. Para pruebas en
paralelo, usá un directorio de datos distinto:

```bash
SUPERFOTO_PORT=8011 SUPERFOTO_DATA_DIR=~/superfoto-data-test scripts/servicio.sh start
```

---

## 10. Seguridad

### 10.1 No tocar el puerto 8000

El puerto `8000` pertenece a `detector-tarjetas`. Este proyecto:

- usa `8010` por defecto;
- documenta la prohibición en la ayuda del script;
- `servicio.sh` **rechaza** `SUPERFOTO_PORT=8000` y valida el rango 1–65535
  antes de arrancar o detener nada;
- `deploy_unoq.sh` **también rechaza** `SUPERFOTO_PORT=8000` explícitamente.

Si necesitás otro puerto, elegí uno libre por encima de 1024 y verificá con
`ss -ltn`.

### 10.2 No matar procesos ajenos

Antes de enviar cualquier señal, `stop` comprueba que el PID corresponda a
`app/server.py` leyendo `/proc/<pid>/cmdline`. Si no coincide, aborta. Lo mismo
hace `start` cuando el puerto está ocupado: solo adopta procesos del proyecto y
nunca mata al intruso.

Esto es importante en la UNO Q porque comparte placa con otros servicios. Un
`kill` a ciegas por puerto podría tumbar `detector-tarjetas` o el propio
sistema.

### 10.3 Secretos y datos

- El proyecto no requiere tokens ni contraseñas para la UI.
- No pongas credenciales en `SUPERFOTO_DATA_DIR`, en la línea del crontab ni en
  la URL de una cámara; el log podría registrarlas.
- `data/` no se versiona (está en `.gitignore`): contiene sesiones, previews y
  logs con rutas y posibles imágenes.
- El crontab es del usuario `arduino`; no lo edites con datos sensibles.
- La UI no tiene autenticación: está pensada para una LAN de confianza. No
  expongas el puerto 8010 a Internet sin poner un proxy con autenticación y
  TLS por delante.

### 10.4 Permisos

El script corre como el usuario dueño del proyecto. Los archivos de estado se
crean con el `umask` habitual (normalmente `022`). Si querés restringir el
log:

```bash
chmod 600 data/server.log
```

No es obligatorio para el funcionamiento, pero evita que otros usuarios de la
placa lean las rutas y los datos de las sesiones.

---

## 11. Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `servicio: no encuentro el interprete 'python3'` | `PYTHON` mal o PATH recortado | `command -v python3`; usá `PYTHON=/usr/bin/python3` |
| `... no puede importar cv2/numpy` | Faltan OpenCV/NumPy | Verificá `python3 -c 'import cv2, numpy'`; en la placa deben venir de `python3-opencv` y `python3-numpy` |
| `el puerto 8010 esta ocupado por otro proceso` | Otro servicio o instancia ajena | `ss -ltnp \| grep :8010`; usá otro puerto o detené el otro servicio por su vía |
| `otra operacion de servicio esta en curso` | Otro `start`/`stop` simultáneo | Esperá y reintentá; el lock se libera solo |
| `status` dice detenido pero el puerto está ocupado | Proceso ajeno o PID file perdido | `pgrep -af app/server.py`; si es nuestro, `start` lo adopta |
| `health: sin respuesta` | Servidor colgado o procesando | `logs 200`; esperá; si persiste, `restart` |
| No arranca: `start` devuelve `ERROR ... no respondio en 10 s` | Arranque fallido, excepción en el servidor | Mirá las últimas 30 líneas que imprime; después `logs 100` |
| `no hay log en ...` | Nunca arrancó o `DATA_DIR` distinto | Verificá `SUPERFOTO_DATA_DIR`; `start` crea el log |
| `logs` no muestra nada o muestra líneas viejas | Log rotado, otro `DATA_DIR` o salida bufferizada | `ls -lh data/server.log*`; usá `logs 500`; confirmá el `DATA_DIR` |
| `stop` dice `no esta corriendo` pero hay proceso | Proceso sin `app/server.py` en cmdline | `pgrep -af superfoto`; no lo mates si no es del proyecto |
| `stop` tarda y envía `KILL` | OpenCV atascado en una operación | Revisá el log; el `KILL` puede dejar una sesión a medias |
| Tras reiniciar no arranca solo | `boot-enable` no ejecutado, cron inactivo o ruta mala | `boot-status`, `systemctl is-active cron`, `crontab -l` |
| `cron` no ejecuta el `@reboot` | Servicio cron detenido o placa sin cron | `systemctl is-active cron` / `service cron status`; pedí que lo habiliten |
| `boot-status` devuelve 1 | No hay línea `@reboot` | `boot-enable` |
| La UI no abre o `status` muestra una IP incorrecta | Bind en localhost, firewall o IP de otra interfaz | Usá la IP de LAN de la placa en `http://<IP_DE_LA_UNO_Q>:8010/`; verificá `SUPERFOTO_HOST=0.0.0.0` |
| `detector-tarjetas` dejó de andar | Alguien tocó el 8000 | Este proyecto no debe hacerlo; revisá el otro servicio y `deploy` rechaza 8000 |
| El deploy y el script se pelean | systemd y cron/script mezclados | Elegí un solo mecanismo; no ejecutes ambos sobre el mismo puerto |

---

## 12. Preguntas frecuentes

**¿Necesito `sudo` para usar `servicio.sh`?**
No. Todo se ejecuta como el usuario `arduino`. Solo la instalación de la
unidad systemd (opcional) requiere `sudo`.

**¿El servicio arranca solo cuando reinicio la placa?**
Solo si ejecutaste `boot-enable`. Sin eso, tenés que correr `start` después de
cada reinicio.

**¿Se reinicia si se cae?**
No. `cron @reboot` solo actúa al arrancar. Para reinicio automático real, usá
la unidad systemd con `Restart=on-failure` (requiere `sudo`).

**¿Puedo usar otro puerto?**
Sí: `SUPERFOTO_PORT=8011`. Usá el mismo valor en todos los comandos y en el
despliegue. Nunca uses 8000.

**¿Dónde están los logs?**
En `data/server.log` (o `$SUPERFOTO_DATA_DIR/server.log`). Se rotan a
`server.log.1` al superar 5 MB.

**¿Cómo actualizo el servicio?**
Con `deploy_unoq.sh` (que reinicia solo) o con `deploy_unoq.sh --no-restart`
seguido de `scripts/servicio.sh restart`.

**¿Qué pasa con las sesiones y videos al actualizar?**
`deploy_unoq.sh` preserva `data/sessions/`, previews y videos. Aun así,
respaldá `data/` antes de cambios grandes.

**¿Puedo correrlo en mi notebook?**
Sí, en Linux con `bash`, `python3`, OpenCV y `flock`. El script usa
`/proc/<pid>/cmdline`, así que no es portable a macOS tal cual.

**¿Por qué no simplemente `python3 app/server.py &`?**
Porque no tendrías PID confiable, health check, lock, rotación de log ni una
forma segura de detenerlo sin matar otro proceso.

**¿Cómo lo desinstalo?**
`scripts/servicio.sh stop` y, si configuraste el arranque, `scripts/servicio.sh
boot-disable`. No hay nada instalado fuera del proyecto ni del crontab del
usuario.

**¿Puedo tener dos instancias?**
Sí, con puertos y `SUPERFOTO_DATA_DIR` distintos. Con el mismo `data/` no: se
pisarían sesiones y previews.

---

## 13. Referencias del repositorio

| Archivo | Qué contiene |
|---|---|
| [`scripts/servicio.sh`](../scripts/servicio.sh) | Implementación del ciclo de vida |
| [`scripts/deploy_unoq.sh`](../scripts/deploy_unoq.sh) | Despliegue SSH sin `sudo`, reinicio y health check |
| [`systemd/superfoto-unoq.service`](../systemd/superfoto-unoq.service) | Unidad systemd de ejemplo (requiere `sudo`) |
| [`app/server.py`](../app/server.py) | Servidor HTTP y endpoint `/api/health` |
| [`tests/test_service_script.py`](../tests/test_service_script.py) | 6 pruebas estáticas y de humo del script |
| [`README.md`](../README.md) | Instalación, uso, API, tests y estructura |
| [`docs/datasheet.md`](datasheet.md) | Especificaciones, operación y mantenimiento |
| [`docs/developer.md`](developer.md) | Arquitectura interna y guía de extensión |
| [`docs/BITACORA.md`](BITACORA.md) | Decisiones y problemas resueltos |
| [`docs/limites-y-alcance.md`](limites-y-alcance.md) | Alcance y convivencia con otros servicios |
| [`docs/REFERENCES.md`](REFERENCES.md) | Referencias externas |

---

*Documento específico de la Arduino UNO Q (Debian 13 `aarch64`, usuario
`arduino`, SSH 2222). Última actualización: 2026-11-14.*
