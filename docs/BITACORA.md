# Bitácora de desarrollo — SuperFoto UNO Q

Registro de las decisiones, los problemas y los caminos descartados durante el
desarrollo del proyecto. Está pensado para que quien retome el trabajo no
repita los mismos intentos ni vuelva a tropezar con los mismos límites.

- **Proyecto:** superresolución por fusión de frames sobre Arduino UNO Q.
- **Estado:** auditoría aprobada sin incidencias; servicio verificado en la
  placa (puerto 8010) con una sesión real de microscopía de 186 frames.
- **Documentos relacionados:** [`../PLAN.md`](../PLAN.md) (especificación),
  [`limites-y-alcance.md`](limites-y-alcance.md) (alcance honesto),
  [`developer.md`](developer.md) (código) y [`datasheet.md`](datasheet.md)
  (uso).

---

## 1. Punto de partida y encuadre del problema

El requerimiento original hablaba de obtener imágenes "10K" o "de microscopio"
a partir de la cámara de un teléfono. Ese encuadre es físicamente imposible: una
cámara con óptica y sensor dados no puede superar su límite de difracción por
software, y ningún algoritmo recupera detalle que no fue capturado.

La decisión fundacional fue **reformular el objetivo** en términos medibles:

1. Aprovechar la diversidad de muestreo subpíxel entre frames (la cámara y la
   escena tiemblan de forma natural).
2. Promediar ruido de sensor y de compresión (√N).
3. Recuperar parte del desenfoque de la PSF con deconvolución.
4. Prometer como máximo **2× lineal** (4× píxeles) y demostrarlo con un
   benchmark reproducible.

Todo el resto del diseño se subordinó a esa promesa. La honestidad del alcance
quedó escrita antes que el código, en [`limites-y-alcance.md`](limites-y-alcance.md).

---

## 2. Decisiones de arquitectura

### 2.1 Lado Linux, no microcontrolador

El pipeline (OpenCV, NumPy, miles de frames, acumuladores de cientos de MB) no
cabe en el STM32U585. Se ejecuta en el lado Linux de la UNO Q (Debian 13
aarch64, Python 3.13, OpenCV 4.10). El sketch del STM32 quedó como **indicador
de estado** en la matriz LED 13×8, sin dependencia del servicio.

**Por qué:** separa responsabilidades, evita bloquear el MCU y permite que la
aplicación funcione aunque el sketch no esté cargado. El enlace
`/dev/ttyHS1` existe, pero no se usa para el flujo principal; es deuda técnica
consciente.

### 2.2 Servidor HTTP con la biblioteca estándar

Se descartó Flask/FastAPI. `http.server.ThreadingHTTPServer` alcanza para una
API pequeña, no agrega dependencias y simplifica el despliegue en la placa
(que ya trae Python y OpenCV del sistema).

**Consecuencia:** hay que escribir a mano el ruteo, el parseo de query y los
tipos MIME. Se hizo con allowlists estrictas y `parse_qs`, no con búsquedas de
substring. La auditoría lo verificó (I-003).

### 2.3 Fuentes de frames enchufables

Se definió un protocolo común (`is_open`, `read`, `stop`, `meta`) con cuatro
implementaciones: IP Webcam (MJPEG + still full-res), archivo de video, webcam
UVC y carpeta de imágenes. La fuente sintética se agregó para tests y
benchmark.

**Por qué:** el proyecto debía poder demostrarse sin cámara y sin teléfono, y
el mismo pipeline debía servir para una sesión real y para un video grabado.

### 2.4 Puerto 8010 y convivencia

En la placa ya corre `detector-tarjetas` en el puerto **8000**. SuperFoto usa
**8010** por defecto y el script de despliegue **rechaza explícitamente** el
8000. Tampoco usa `sudo` ni reinicia servicios ajenos.

**Por qué:** el mayor riesgo del proyecto no era algorítmico sino romper un
servicio en producción. Esa restricción se convirtió en una validación del
script y en un test de convivencia.

---

## 3. Problemas encontrados y cómo se resolvieron

### 3.1 El acumulador `uint8` saturaba

**Síntoma:** con muchos frames, el contador de muestras por píxel de salida
saturaba y la imagen se lavaba en las zonas de alta cobertura.

**Causa:** el contador de drizzle se guardaba en `uint8` (máximo 255).

**Fix:** `np.uint16` con incremento saturado en `_MAX_COUNT = 65535`, más un
test con 400 frames que verifica el límite. Quedó registrado como I-001.

### 3.2 El "zoom 8×" de la UI se interpretaba como óptico

**Síntoma:** el visor permite ampliar hasta 800 %, y eso se confundía con
ganancia real de resolución.

**Fix:** la UI separa dos acciones distintas:

- **Recorte 1:1 visible:** píxeles reales de la superfoto, sin interpolar.
- **Recorte central 4×:** vecino más cercano, explícitamente digital.

Además, la documentación repite que el zoom es una lupa, no un microscopio.
Ver sección 3 de [`limites-y-alcance.md`](limites-y-alcance.md).

### 3.3 La fusión online sola quedaba por debajo del bicúbico

**Síntoma:** en el benchmark, el drizzle ponderado sin refinamiento daba
23.48 dB, peor que el bicúbico de un solo frame (24.34 dB). El ruido bajaba,
pero la imagen quedaba más blanda.

**Diagnóstico:** promediar muestras reduce ruido, pero no revierte la PSF
óptica ni la interpolación. Hacía falta un paso que reinyecte la información de
los frames de baja resolución.

**Fix:** se agregó *iterative back-projection* (IBP) sobre una muestra acotada
de frames (hasta 32, 96 MB) y luego Richardson-Lucy sobre luminancia. El
benchmark subió a 29.76 dB con IBP y 30.81 dB con RL (+6.47 dB sobre bicúbico).

**Lección:** el promedio por sí solo no es superresolución. El orden
drizzle → IBP → RL es lo que produce la ganancia.

### 3.4 El video "real" de 50 s no servía

**Síntoma:** de 1250 frames vistos solo se aceptaron **7**, con un shift medio
de 10.2 px y cobertura 16× nula. La imagen resultante no mejoraba nada.

**Causa:** la cámara se movía (no solo temblaba): el campo se desplazaba entre
frames más de lo que el alineador podía corregir.

**Fix:** en lugar de forzar la fusión, se agregó un **aviso de escena en
movimiento** cuando la proporción de alineaciones inválidas supera el 50 %
(mínimo 30 frames). La sesión se guarda igual, pero con una advertencia
explícita. El caso quedó documentado como contraejemplo en
[`limites-y-alcance.md`](limites-y-alcance.md).

### 3.5 Hacía falta una prueba real que no fuera sintética

**Síntoma:** el benchmark sintético es honesto, pero no convence de que el
sistema funcione con una cámara real y un objeto real.

**Fix:** se grabó un video estable de 19 s de un estoma vegetal en 1080p y se
procesó en la placa. Resultado: 487 frames vistos, **186 aceptados**, salida
**3840×2160**, 459.5 s de captura, 68.6 s de postproceso, shift medio
**0.66 px** (máximo 1.55 px) y cobertura 100 %. La comparativa visual
(`docs/img/compare_stoma.jpg` y `compare_stoma_detail.jpg`) muestra el frame
único ampliado a la izquierda y la fusión de 186 frames a la derecha.

**Lección:** la estabilidad del montaje importa más que la duración. 19 s
estables dieron mejor resultado que 50 s con la cámara moviéndose.

### 3.6 `/dev/video0` y `/dev/video1` no son cámaras

**Síntoma:** al listar dispositivos de video en la UNO Q aparecen
`/dev/video0` y `/dev/video1`, pero abrirlos no entrega imagen.

**Causa:** son decodificadores **Venus** de Qualcomm (aceleración de video),
no sensores UVC.

**Fix:** `WebcamSource` lee el nombre en sysfs
(`/sys/class/video4linux/videoN/name`) y descarta los que contienen `venus`,
`decoder`, `qcom`, `qualcomm`, `encod` o `codec`. Si no queda ninguna cámara
real, lanza un error en español que lista los dispositivos encontrados.

### 3.7 Guardado no atómico y sesiones incompletas

**Síntoma:** si el proceso moría a mitad de escritura, quedaba una carpeta de
sesión con archivos parciales y un `session.json` que prometía artefactos
inexistentes.

**Fix:** escritura atómica `.tmp` + `fsync` + `os.replace` para cada archivo, y
`session.json` **al final** como marcador de completitud. Si algo falla, se
borra la carpeta incompleta. Además, `min_free_mb` (500 MB por defecto) aborta
antes de llenar el disco, con limpieza agresiva de sesiones viejas.

### 3.8 La API filtraba rutas absolutas

**Síntoma:** las respuestas de listado incluían rutas del sistema
(una ruta absoluta del home), lo que rompía la portabilidad y exponía estructura interna.

**Fix:** `_describe()` devuelve solo `rel` (`sessions/<id>`) y URLs de API. Los
tests verifican que no aparezca ninguna ruta absoluta del home. Quedó como I-002.

### 3.9 Condiciones de carrera en el gate de calidad

**Síntoma:** el hilo de captura actualizaba el umbral adaptativo mientras la API
lo leía; esporádicamente se veían valores inconsistentes.

**Fix:** `QualityGate` protege todo el estado mutable con `RLock`, incluida la
ventana de historial y los contadores de rechazo. Hay un test concurrente.

### 3.10 El script de prueba real borraba archivos locales

**Síntoma:** el script pensado para limpiar el video remoto usaba un `rm` que,
con una variable vacía, podía apuntar al equipo local.

**Fix:** el borrado se hace **solo por SSH** y solo del archivo remoto
copiado; nunca toca archivos locales. Quedó como I-006.

### 3.11 Otros hallazgos de auditoría

- `--max-frames` aceptaba negativos: se agregó `non_negative_int` (I-007).
- La UI construía el historial con `innerHTML`: se reemplazó por
  `createElement` + `textContent` (I-008).
- `query_int` usaba búsqueda de substring: se pasó a `urllib.parse.parse_qs`
  (I-003).
- Faltaba validar espacio libre antes de guardar: se agregó `min_free_mb`
  (I-009).

Ninguno de estos fixes cambió el algoritmo; todos endurecieron bordes.

### 3.12 Script de servicio y arranque automático (2026-11-14)

**Contexto.** `deploy_unoq.sh` dejaba el servidor corriendo con `nohup`, pero
no había una interfaz uniforme para detenerlo, consultarlo o reiniciarlo sin
`sudo`. Tampoco había arranque automático tras un reinicio de la placa.

**Decisión.** Se agregó `scripts/servicio.sh` (333 líneas, `bash -n` OK) con
`start`, `stop`, `restart`, `status`, `logs [N]`, `boot-enable`,
`boot-disable`, `boot-status` y `help`. Usa PID file, `flock`, health check
HTTP y `nohup`; no toca systemd ni el puerto 8000. La unidad systemd queda como
alternativa que requiere `sudo`.

**Bug del lock.** El servidor se lanzaba dentro de un subshell que heredaba el
descriptor `9` del `flock`. Mientras el servidor vivía, el lock quedaba tomado
y `stop`/`restart` fallaban con “otra operacion de servicio esta en curso”. Se
corrigió cerrando el descriptor en el hijo (`nohup ... 9>&- &`). **Verificado
en la placa:** `stop`, `start`, `restart` y otro ciclo `stop`/`start`
funcionaron sin el error.

**IP de `status`.** `hostname -I` devolvía primero la bridge de Docker, no la
IP de LAN. Se cambió a la IP de la ruta por defecto (`ip -4 route get`), con
fallback a `hostname -I` y `127.0.0.1`. En la documentación se usa siempre
`<IP_DE_LA_UNO_Q>`, nunca una IP real.

**Arranque automático.** `boot-enable` agrega una sola línea `@reboot` al
crontab del usuario, etiquetada `# superfoto-unoq-service`; es idempotente y
no requiere `sudo`. `boot-status` confirma el estado y `boot-disable` la
quita. Requiere que el servicio `cron` esté activo.

**Verificación real en la UNO Q** (PID efímeros):

| Paso | Resultado |
|---|---|
| `stop` | puerto 8010 libre |
| `start` | PID 12906, health OK |
| `restart` | PID 13046, sin error de lock |
| `stop` + `start` | PID 13206 |
| `status` | estado, URL y health correctos |
| `logs 6` | últimas 6 líneas |
| `boot-enable` ×2 | una sola línea en el crontab |
| `boot-status` | habilitado |
| `detector-tarjetas` | intacto en 8000 |

**Pruebas.** Se agregó `tests/test_service_script.py` con 6 pruebas
(existencia/ejecutable, `bash -n`, comandos/guardas, `help`, `status` con data
dir temporal y prohibición del 8000). La suite pasó de 99 a **105 tests**.

**Seguridad.** El script valida el PID contra `/proc/<pid>/cmdline` antes de
matar, solo adopta procesos de `app/server.py`, no hardcodea IPs y nunca toca
el 8000.

---

## 4. Enfoques descartados (y por qué no reintentarlos)

| Enfoque | Por qué se descartó |
|---|---|
| **Upscaling de un solo frame** (bicúbico, Lanczos, ESRGAN) | No agrega información: inventa textura plausible. El benchmark lo usa solo como línea base. |
| **Superresolución generativa / IA** | Puede producir detalle verosímil pero falso. Inaceptable para microscopía o documentación técnica. |
| **Prometer "10K" o "microscopio"** | Físicamente imposible con la óptica del teléfono. Se reemplazó por 2× lineal medido. |
| **Fusión online sola (drizzle puro)** | Quedaba por debajo del bicúbico en PSNR/SSIM; falta reinyección de altas frecuencias. |
| **Alinear con phase correlation solamente** | Precisión típica ~0.2 px; insuficiente para 16 muestras por píxel. Se agregó refinamiento ECC. |
| **ECC sobre el frame completo** | Caro y frágil; se hace sobre un recorte central de ≤360 px, inicializado con la fase. |
| **Guardar todos los frames en RAM** | Con 1080p y cientos de frames no entra. Se usa acumulador online + buffer acotado para IBP. |
| **Flask / FastAPI** | Dependencia innecesaria para una API de una sola sesión; la stdlib alcanza. |
| **Montar el servicio con `sudo` / systemd del sistema** | La placa ya tiene un servicio ajeno (detector-tarjetas). Se prefirió despliegue de usuario y unidad systemd de ejemplo. |
| **Usar `/dev/video0` como cámara** | Es un decodificador Venus, no un sensor. Se filtra por sysfs. |
| **Aceptar frames borrosos "porque hay muchos"** | Degradan la fusión; el gate por percentil de nitidez es parte del diseño (lucky imaging). |
| **Procesar video con movimiento de cámara** | El alineador es de traslación (fase) + euclidiano (ECC); un paneo grande no se corrige. El sistema avisa en vez de inventar. |
| **Etiquetas con acentos en `compare.jpg`** | `cv2.putText` no maneja UTF-8; se usan etiquetas ASCII. |

---

## 5. Decisiones algorítmicas que conviene recordar

1. **Escala efectiva 1.5–2.0.** Por debajo de 1.5 se desperdicia diversidad
   subpíxel; por encima de 2.0 se sobremuestrea y crece el costo sin ganancia
   real.
2. **Pesos por nitidez y exposición.** No todos los frames aportan igual:
   `w = clip((sharpness/threshold)^0.5, 0.3, 3.0) ×
   clip(1 - clipped_ratio×2, 0.4, 1.0)`.
3. **Máscara de cobertura.** Las zonas sin muestras se rellenan con el
   bicúbico de referencia; no se dejan agujeros.
4. **RL solo en luminancia.** Aplicarlo en RGB amplifica el ruido de croma.
   El croma se suaviza aparte con un bilateral a media resolución.
5. **PSF gaussiana de 0.6 px.** Es una aproximación; el valor correcto depende
   de la óptica y la distancia. Se expone como parámetro.
6. **IBP con pocos frames.** Usar todos los frames en cada iteración sería
   prohibitivo en RAM y tiempo; 16–32 frames bien distribuidos bastan.
7. **Umbral adaptativo por percentil.** El percentil 60 de la nitidez de la
   sesión se adapta a la escena; un umbral absoluto falla entre objetos.
8. **Cota de memoria antes que cota de calidad.** Si la escala pedida excede
   32 MP o 700 MB de acumuladores, se reduce automáticamente y se informa.

---

## 6. Pruebas y validación

- **Suite:** 105 funciones de test en `tests/` (99 del núcleo + 6 del script
  de servicio), sin dependencias externas de pytest más allá de lo estándar.
  Cubren storage, alineación, sesión, API, fuente sintética, calidad, CLI, el
  sketch y `scripts/servicio.sh`.
- **Benchmark:** `scripts/benchmark_sr.py` genera un ground truth
  **banda-limitado** (no un patrón perfecto), lo degrada con PSF, jitter,
  ruido σ=3 y JPEG q=88, y compara bicúbico vs. fusión. Criterio de aprobación:
  ≥1.5 dB de ganancia **y** mejor SSIM.
- **Resultado:** +6.47 dB en el benchmark (24.34 → 30.81 dB; SSIM 0.9214 →
  0.9497) y +5.31 dB en la placa.
- **Prueba real:** 186 frames aceptados, 3840×2160, cobertura 100 %,
  459.5 s de captura y 68.6 s de postproceso.

El benchmark es deliberadamente conservador: si el ground truth tuviera detalle
por encima del límite de Nyquist del sensor, cualquier método "ganaría" por
artefacto. Banda-limitarlo evita esa trampa.

---

## 7. Lecciones aprendidas

1. **Definir el límite físico antes de programar** evita promesas imposibles y
   discusiones estériles. La sección de alcance se escribió temprano y se
   respetó.
2. **El movimiento de la escena es el enemigo principal**, no el ruido. Un video
   corto y estable vale más que uno largo y tembloroso.
3. **La ganancia viene del conjunto**, no de una etapa mágica: muestreo
   subpíxel + alineación fina + reinyección de altas frecuencias + deconvolución.
4. **La memoria es el límite práctico** en una placa de 3.6 GB. Hay que
   presupuestarla desde el diseño (acumuladores, buffer IBP, sesiones en disco).
5. **La convivencia con servicios existentes** es un requisito de primer nivel:
   puerto propio, sin sudo, sin tocar el 8000.
6. **La documentación honesta es parte del producto.** Un README que promete
   microscopía genera decepción; uno que explica 2× y √N genera confianza.
7. **Los bordes importan:** saturación de contadores, escritura atómica, rutas
   absolutas, carreras de hilos. La auditoría encontró diez y todas eran
   corregibles sin tocar el núcleo.
8. **Un contraejemplo vale tanto como un éxito.** La sesión de 50 s con 7
   frames justifica el aviso de movimiento mejor que cualquier explicación.
9. **El ciclo de vida es parte del producto.** Un servidor que arranca pero no
   se puede reiniciar con seguridad es una bomba de tiempo operativa.
10. **Probar `restart`, no solo `start`.** El bug del `flock` heredado solo
    apareció al detener y volver a arrancar; los tests de humo ahora cubren el
    cierre de `9>&-`.

---

## 8. Deuda técnica y próximos pasos

- **[ ] Enlace MCU–Linux por `/dev/ttyHS1`.** El sketch es autónomo; podría
  reflejar el estado real de la sesión (capturando/procesando/listo) si el
  servidor escribiera por el puerto serie.
- **[ ] Alineación afín completa.** Hoy es traslación + euclidiana; rotaciones
  y cambios de escala pequeños no se modelan.
- **[ ] Autenticación.** La API no tiene auth ni TLS; está pensada para LAN
  confiable.
- **[ ] Sesiones concurrentes.** Hay una sola sesión activa a la vez (409 si se
  intenta otra).
- **[ ] Selección de frames por diversidad.** El gate es por nitidez; un
  criterio de cobertura subpíxel podría mejorar la fusión.
- **[ ] Exportar TIFF de 16 bits.** Hoy la salida es PNG/JPEG de 8 bits.
- **[ ] Tests de integración con cámara real.** La suite usa fuentes sintéticas
  y archivos; no hay un test automático con hardware.
- **[ ] Borrado de sesiones desde la UI.** Solo se limpian automáticamente por
  retención o espacio.
- **[ ] Medición de PSF.** El valor 0.6 px es una aproximación; una calibración
  con patrón permitiría ajustarlo por óptica.

---

## 9. Cronología resumida

| Etapa | Hito |
|---|---|
| 1 | Reformulación del objetivo a 2× lineal y benchmark honesto. |
| 2 | Pipeline base: fuentes, gate, alineación, drizzle, postproceso. |
| 3 | UI web, API y CLI; despliegue sin sudo en el puerto 8010. |
| 4 | Sketch de la matriz LED 13×8 y estados I/C/P/D. |
| 5 | Primer benchmark: +6.47 dB; se detecta que el drizzle solo no alcanza. |
| 6 | Se agregan IBP y RL; sube PSNR/SSIM y se estabiliza el pipeline. |
| 7 | Prueba real de 19 s: 186 frames, 3840×2160, shift 0.66 px. |
| 8 | Auditoría: 10 incidencias corregidas; aprobada sin incidencias. |
| 9 | Documentación final: README, datasheet, developer, límites y esta bitácora. |
| 10 | `scripts/servicio.sh`: start/stop/restart/status/logs sin sudo; fix del lock heredado. |
| 11 | Arranque automático por cron (`boot-enable`) y verificación real en la UNO Q. |

---

*Última actualización: 2026-11-14.*
