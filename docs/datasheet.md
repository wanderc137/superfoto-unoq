# SuperFoto UNO Q — Hoja de datos de usuario

**Versión del software:** 1.0.0
**Plataforma:** Arduino UNO Q (lado Linux Debian 13 aarch64 + STM32U585 con Zephyr)
**Puerto de servicio:** 8010/TCP
**Estado:** auditoría aprobada, sin incidencias; probado en hardware real

---

## 1. Qué es

SuperFoto UNO Q es una aplicación que corre en la Arduino UNO Q y construye una
**superfotografía** a partir de un video de un objeto inmóvil. Toma cientos o
miles de frames, descarta los borrosos, alinea los buenos con precisión
subpíxel y los fusiona en una imagen final con:

- hasta **2× de resolución lineal** (4× de píxeles) sobre la fuente;
- **menos ruido** (promediado de √N frames);
- **más contraste fino** por deconvolución de la PSF óptica.

Se opera desde el navegador, por API HTTP o por línea de comandos. No requiere
instalar dependencias de Python: usa la stdlib, OpenCV y NumPy del sistema.

### Para qué sirve

- Microscopía de aficionado: hojas, insectos, textiles, muestras de agua.
- Macrofotografía de objetos planos o inmóviles.
- Documentación técnica de piezas, circuitos o superficies.
- Procesamiento por lotes de videos ya grabados.

### Para qué no sirve

- No es un microscopio óptico ni aumenta el poder de resolución del objetivo.
- No sirve para escenas en movimiento, personas, mascotas o calle.
- No corrige enfoque: si el video está borroso, el resultado será borroso.
- No reemplaza un escáner de documentos ni una cámara de laboratorio.
- No "inventa" texto ilegible ni detalle por debajo del límite de difracción.

> La explicación completa de los límites, con mediciones, está en
> [`limites-y-alcance.md`](limites-y-alcance.md).

---

## 2. Especificaciones

### 2.1 Hardware

| Elemento | Especificación |
|---|---|
| Placa | Arduino UNO Q |
| MCU | STM32U585xx, ARM Cortex-M33 con TrustZone |
| RAM MCU | 262 KB |
| Flash MCU | 2 MB |
| SoC Linux | QCC711, Debian 13 aarch64 |
| RAM Linux | 3.6 GB (≈2.4 GB libres en las pruebas) |
| Almacenamiento | 9.8 GB eMMC (≈771 MB libres en las pruebas) |
| Lógica | 3.3 V |
| Matriz LED | 13×8, 3 bits de gris (0–7) |
| RTOS del sketch | Zephyr |
| FQBN | `arduino:zephyr:unoq` |

### 2.2 Software

| Elemento | Versión / nota |
|---|---|
| Python | 3.13 (stdlib solamente) |
| OpenCV | 4.10 (`python3-opencv`) |
| NumPy | 2.2 (`python3-numpy`) |
| Servidor HTTP | `http.server.ThreadingHTTPServer` |
| Dependencias externas | **Ninguna** (sin Flask, FastAPI, scipy, Pillow, scikit-image) |
| Navegador | Chrome, Firefox o Edge modernos |
| Suite de pruebas | 105 tests (`python3 -m pytest -q`) |

### 2.3 Fuentes de imagen

| Fuente | Alias | Requisito |
|---|---|---|
| IP Webcam (Android) | `ip` | Teléfono y placa en la misma LAN; URL `http://<IP_DEL_TELEFONO>:8080` |
| Archivo de video | `file` | Cualquier formato que decodifique OpenCV |
| Carpeta de imágenes | `folder` | Archivos ordenados alfabéticamente |
| Webcam USB (UVC) | `webcam` | Cámara UVC real conectada a la placa |
| Demo sintética | `synthetic` | Sin hardware; genera ground truth |
| Automática | `auto` | Prueba URL configurada → busca en LAN → archivo configurado |

### 2.4 Entradas y salidas

| Entrada | Rango / formato |
|---|---|
| Resolución de captura | La que entregue la fuente (típico 1280×720 o 1920×1080) |
| Escala de salida | 1.0× a 2.0× |
| Píxeles máximos de salida | 32 000 000 (32 MP) |
| Memoria máxima de acumuladores | 700 MB |
| Frames objetivo | 0 (sin límite) a 5000 en la UI |
| Duración máxima de captura | 300 s por defecto (`max_elapsed_s`), configurable |
| Formato de salida | PNG 8-bit sin pérdida; JPEG q95 |
| Artefactos | `superfoto.png`, `superfoto.jpg`, `thumb.jpg`, `compare.jpg`, `reference.jpg`, `coverage.png`, `session.json` |

### 2.5 Rendimiento medido en la placa

| Fuente | Salida | Frames | Captura + fusión | Postproceso |
|---|---|---|---|---|
| 720p (1280×720) | 1920×1080 | 90 | 38.9 s (2.3 fps) | 24.8 s |
| 1080p (1920×1080) | 2880×1620 | 45 | 26.8 s (1.7 fps) | 36.0 s |
| 1080p, prueba real | 3840×2160 | 186 | 459.5 s (incluye lectura de video) | 68.6 s |

El postproceso es un costo fijo porque trabaja sobre un buffer acotado de
15–32 frames, no sobre la sesión completa.

### 2.6 Límites de sesión

| Límite | Valor |
|---|---|
| Sesiones conservadas | 3 (las más recientes) |
| Espacio libre mínimo | 500 MB |
| Sesiones simultáneas | 1 |
| Tamaño máximo de JSON de entrada | 1 MB |
| Desplazamiento máximo aceptado | 32 px |
| Correlación mínima | 0.35 |

---

## 3. Requisitos

### 3.1 Para usar la placa

- Arduino UNO Q con Debian 13 aarch64 y OpenCV/NumPy del sistema.
- Fuente de alimentación por USB-C.
- Red WiFi o Ethernet para la UI y la cámara IP.
- Navegador en otro equipo de la misma red (o en la propia placa).
- Objeto inmóvil, iluminación estable y cámara fija.

### 3.2 Para desarrollar en el host

- Python 3.10 o superior con `opencv-python` y `numpy`.
- No hace falta el resto de las dependencias.
- Para el sketch: `arduino-cli` con el core `arduino:zephyr` y la placa
  conectada por USB-C.

### 3.3 Para el sketch

- `arduino-cli` 1.x.
- Core `arduino:zephyr`.
- `adb` funcionando y regla udev para el USB ID `2341:0078` si hace falta.
- La app Linux **no depende** del sketch: si no se carga, todo lo demás anda.

---

## 4. Instalación

### 4.1 Despliegue en la placa (recomendado, sin sudo)

Desde el host, en la raíz del proyecto:

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q> scripts/deploy_unoq.sh
```

El script copia el código por SSH, preserva `data/sessions`, reinicia el
servicio y verifica `/api/health`. No usa `sudo`, no usa `rsync` y rechaza
explícitamente el puerto 8000.

Variables:

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q>
REMOTE_PORT=2222
REMOTE_USER=arduino
REMOTE_DIR=~/superfoto-unoq
SSH_KEY=~/.ssh/unoq_key
SUPERFOTO_PORT=8010
```

### 4.2 Operación en la placa con `scripts/servicio.sh`

El despliegue deja el servidor corriendo. Para operarlo sin `sudo`:

```bash
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq

scripts/servicio.sh status       # estado, PID, URL y health
scripts/servicio.sh start        # idempotente; adopta el proceso del deploy
scripts/servicio.sh stop         # TERM y, si hace falta, KILL
scripts/servicio.sh restart
scripts/servicio.sh logs 50
```

La UI queda en `http://<IP_DE_LA_UNO_Q>:8010/`. Comprobación directa:

```bash
curl -s http://<IP_DE_LA_UNO_Q>:8010/api/health
```

Respuesta esperada (resumida):

```json
{
  "ok": true,
  "name": "superfoto-unoq",
  "version": "1.0.0",
  "state": "idle",
  "session_active": false,
  "data_dir_name": "data",
  "disk": {"free_mb": 771, "min_free_mb": 500, "low_space": false},
  "python": "3.13.x",
  "opencv": "4.10.0"
}
```

El script valida el PID contra `/proc/<pid>/cmdline`, usa un lock para evitar
operaciones simultáneas, no mata procesos ajenos y nunca toca el puerto 8000.
La referencia completa está en [`servicio.md`](servicio.md).

Si el script no estuviera disponible, el arranque manual de emergencia es:

```bash
python3 app/server.py --host 0.0.0.0 --port 8010
```

### 4.3 Arranque automático con cron (sin sudo)

```bash
scripts/servicio.sh boot-enable
scripts/servicio.sh boot-status
scripts/servicio.sh boot-disable
```

`boot-enable` agrega una única línea `@reboot` al crontab del usuario
`arduino`; es idempotente. No requiere `sudo` ni systemd, pero **cron debe
estar activo**:

```bash
systemctl is-active cron
# active
```

Si cron no corre, la línea existe pero no se ejecuta. `boot-status` devuelve
`0` cuando está habilitado y `1` cuando no. Para un puerto distinto, editá la
línea del crontab y agregá `SUPERFOTO_PORT=8011` antes del script.

### 4.4 Alternativa systemd (requiere sudo)

La unidad [`../systemd/superfoto-unoq.service`](../systemd/superfoto-unoq.service)
es un ejemplo. Instalarla requiere `sudo`, que el proyecto evita por diseño:

```bash
sudo cp systemd/superfoto-unoq.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now superfoto-unoq
```

No mezcles systemd con `servicio.sh` sobre el mismo puerto.

### 4.5 Desarrollo local

```bash
scripts/run_local.sh synthetic
scripts/run_local.sh webcam
scripts/run_local.sh ip http://<IP_DEL_TELEFONO>:8080
```

---

## 5. Operación

### 5.1 Interfaz web

Abrir `http://<IP_DE_LA_UNO_Q>:8010/`.

La pantalla tiene tres zonas:

1. **Captura:** fuente y bloque **Conexión IP Webcam** (campo *IP o URL del
   teléfono*, botones **Probar conexión** y **Buscar cámara**), más ruta o
   índice según la fuente.
2. **Parámetros:** escala, percentil de nitidez, PSF, iteraciones RL, duración
   y frames objetivo; presets **Prueba real (300 s)** y **Demo rápida**.
3. **Resultado / Preview:** visor con zoom y pan, descargas, progreso y log de
   actividad.

El campo de IP acepta `<IP_DEL_TELEFONO>`, `<IP_DEL_TELEFONO>:8080` o la URL completa; el
servidor la normaliza (esquema `http://` y puerto `8080` si faltan) y
**Probar conexión** confirma con la resolución real (`Conectado: 1280x720`). La
última IP se recuerda en el navegador. Funciona con cualquier teléfono, sin
reconfigurar la placa.

Indicadores de progreso:

| Indicador | Significado |
|---|---|
| Vistos | Frames leídos de la fuente |
| Aceptados | Frames que pasaron el gate y se fusionaron |
| Rechazados | Frames descartados por calidad o alineación |
| Alin. inválida | Frames con shift excesivo o baja correlación |
| Umbral nitidez | Umbral adaptativo actual |
| Cobertura | Porcentaje del área con muestras suficientes |
| Salida | Tamaño estimado de la imagen final |

### 5.2 Flujo recomendado de una sesión real

1. Fijar la cámara y el objeto.
2. Iluminar de forma difusa y estable.
3. Abrir IP Webcam en el teléfono y verificar `http://<IP_DEL_TELEFONO>:8080`.
4. En la UI, elegir **IP Webcam**, pegar la URL y pulsar **Prueba real
   (300 s)**.
5. Pulsar **Iniciar** y no tocar nada durante la captura.
6. Al terminar, esperar el postproceso (el estado pasa a `processing`).
7. Revisar la comparativa 1:1 y descargar el PNG.
8. Guardar el `session.json` si se necesita trazabilidad.

### 5.3 Presets

| Preset | Fuente | Duración | Frames | Escala | Percentil |
|---|---|---|---|---|---|
| Prueba real | IP Webcam | 300 s | 0 (todos) | 1.5 | 55 |
| Demo rápida | Sintética | 30 s | 60 | 2.0 | 40 |

### 5.4 Descargas

| Archivo | Contenido |
|---|---|
| `superfoto.png` | Resultado sin pérdida, 8-bit |
| `superfoto.jpg` | Resultado en JPEG q95 |
| `compare.jpg` | Comparativa de la zona con más detalle, ampliada 2x: bicúbico de 1 frame vs fusión, con la ganancia de detalle medida en el rótulo |
| `reference.jpg` | Frame de referencia (el primero aceptado) |
| `coverage.png` | Mapa de cobertura subpíxel |
| `thumb.jpg` | Miniatura de 320 px |
| `session.json` | Métricas, parámetros, rutas relativas y nombres de archivo |

### 5.5 Modos de fallo visibles

| Estado | Significado |
|---|---|
| `idle` | Sin sesión |
| `starting` | Abriendo la fuente |
| `capturing` | Capturando y fusionando |
| `processing` | Postproceso (IBP + RL) |
| `finished` | Resultado listo |
| `error` | Fallo con mensaje en `error` |
| `stopped` | Detenida por el usuario |

---

## 6. API HTTP (resumen)

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/health` | Diagnóstico |
| GET | `/api/status` | Progreso |
| GET | `/api/sources` | Fuentes y defaults |
| POST | `/api/session/start` | Iniciar |
| POST | `/api/session/stop?wait=1` | Detener y esperar |
| GET | `/api/session/latest` | Último resultado |
| GET | `/api/sessions` | Historial |
| GET | `/api/preview` | Último frame aceptado |
| GET | `/api/probe?url=<ip\|url>` | Probar la IP/URL del teléfono (200/400/502) |
| GET | `/api/discover` | Buscar IP Webcam |
| GET | `/api/result/<id>/<key>` | Descargar artefacto |

Ejemplo mínimo:

```bash
# Probar el telefono antes de capturar
curl -s 'http://<IP_DE_LA_UNO_Q>:8010/api/probe?url=<IP_DEL_TELEFONO>:8080'

curl -s -X POST http://<IP_DE_LA_UNO_Q>:8010/api/session/start \
  -H 'Content-Type: application/json' \
  -d '{"source":"ip","args":{"url":"http://<IP_DEL_TELEFONO>:8080"},"duration_s":300,"scale":1.5}'
```

La referencia completa está en [`developer.md`](developer.md).

---

## 7. Mantenimiento

### 7.1 Sesiones y disco

- Se conservan las **3 sesiones** más recientes.
- Antes de guardar se exigen **500 MB libres**. Si no alcanzan, se purgan
  sesiones viejas; si aun así no alcanza, la sesión falla con un mensaje claro.
- Las escrituras son atómicas: no quedan archivos a medias.
- Para liberar espacio manualmente:

```bash
ls -la data/sessions/
rm -rf data/sessions/<id_viejo>
```

No borres `data/server.log` mientras el servicio corre (podés truncarlo).

### 7.2 Logs

- En la placa: `data/server.log`.
- Lectura rápida: `scripts/servicio.sh logs 100` (o `logs 500`).
- Seguimiento en vivo: `tail -f data/server.log`.
- Rotación: al superar **5 MB**, el script lo renombra a `data/server.log.1`.
- En arranque manual: stderr de la terminal.
- Niveles: `DEBUG`, `INFO`, `WARNING`, `ERROR` (`--log-level` o
  `SUPERFOTO_LOG_LEVEL`).

### 7.3 Actualización

```bash
REMOTE_HOST=<IP_DE_LA_UNO_Q> scripts/deploy_unoq.sh
```

El despliegue detiene el servicio, copia el código, lo reinicia y verifica
`/api/health`; preserva `data/sessions`, `data/previews` y `data/videos`.
Si usaste `--no-restart`, completá el reinicio en la placa:

```bash
ssh -p 2222 arduino@<IP_DE_LA_UNO_Q>
cd ~/superfoto-unoq
scripts/servicio.sh restart
```

### 7.4 Respaldo

Los resultados están en `data/sessions/<id>/`. Copiá esa carpeta (o el
`session.json` + PNG) para conservar una sesión. No hace falta respaldar
`data/previews/`: es descartable.

---

## 8. Límites y advertencias

- **Ganancia máxima realista:** ~2× lineal (4× píxeles) + reducción de ruido
  √N + recuperación parcial de PSF. No hay milagros.
- **Objeto inmóvil:** si se mueve, la alineación falla y el sistema avisa. En
  un video inestable se pueden aceptar muy pocos frames (se midió 7 de 1250).
- **Iluminación estable:** el pipeline no compensa cambios de exposición.
- **Zoom 8× digital:** no agrega información.
- **32 MP de tope:** la escala se reduce automáticamente si no entra en
  memoria, y se informa la escala efectiva.
- **Sin autenticación:** solo LAN de confianza.
- **Puerto 8000 reservado:** pertenece a `detector-tarjetas`; este proyecto no
  lo toca.
- **3.3 V en los headers:** no conectar 5 V directo.
- **ADC de 12 bits:** no aplica a este proyecto, pero es una diferencia clave
  con el Uno clásico.
- **Zephyr, no AVR:** las librerías específicas de AVR no compilan en la UNO Q.

---

## 9. Preguntas frecuentes

**¿Necesito instalar algo en la placa?**
No. Python, OpenCV y NumPy ya están en Debian. El proyecto no usa pip.

**¿Puedo usar la cámara del teléfono por USB en vez de WiFi?**
Sí, si el teléfono se presenta como UVC; lo normal es usar IP Webcam por WiFi.
La webcam USB también funciona si es UVC estándar.

**¿Por qué `/dev/video0` no funciona en la UNO Q?**
Porque es un decodificador Qualcomm Venus, no una cámara. El proyecto lo
detecta y lo descarta. Usá IP Webcam o una UVC real.

**¿Cuántos frames necesito?**
Para una escena estable, 100–300 frames aceptados dan una mejora clara. Más
frames reducen ruido, pero el costo de captura crece linealmente. La prueba
real usó 186.

**¿Qué escala elijo?**
1.5 es el equilibrio; 2.0 para máxima ampliación. Por encima de 2.0 no se
permite.

**¿Por qué el resultado se ve más "duro"?**
Por la deconvolución. Si aparece ruido o halos, bajá RL a 10 o subí `psf_sigma`
a 0.7–0.8.

**¿Puedo procesar un video viejo?**
Sí: `scripts/process_video.py --input video.mp4` o la fuente "Archivo de
video" en la UI.

**¿Se puede usar en movimiento?**
No. El sistema avisa cuando más de la mitad de los frames no alinean y fusiona
solo el segmento estable. Para movimiento real hace falta otra técnica
(estabilización, video, no multi-frame SR).

**¿Dónde veo las métricas?**
En `session.json` y en el panel de progreso. Incluye PSNR/SSIM solo cuando la
fuente es sintética y hay ground truth.

**¿Es seguro exponerlo a Internet?**
No. No tiene autenticación ni TLS. Usalo en LAN o detrás de un proxy con
autenticación.

---

## 10. Glosario

| Término | Definición |
|---|---|
| **Frame** | Cuadro individual de un video |
| **Lucky imaging** | Seleccionar solo los frames menos distorsionados |
| **Drizzle** | Depositar muestras en una grilla más fina en vez de interpolar |
| **PSF** | Función de dispersión de punto; describe el desenfoque óptico |
| **IBP** | Iterative Back-Projection; refinamiento por consistencia con los datos |
| **Richardson-Lucy** | Deconvolución iterativa para recuperar detalle atenuado por la PSF |
| **ECC** | Enhanced Correlation Coefficient; registro de imágenes |
| **Shift** | Desplazamiento estimado entre un frame y la referencia |
| **Cobertura** | Fracción del área de salida con suficientes muestras |
| **Ground truth** | Imagen ideal conocida, usada solo en el benchmark sintético |
| **SSIM** | Índice de similitud estructural (0–1, más alto mejor) |
| **PSNR** | Relación señal/ruido de pico, en dB (más alto mejor) |
