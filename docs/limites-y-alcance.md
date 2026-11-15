# Límites y alcance real de SuperFoto UNO Q

Documento corto para no vender humo: qué se puede esperar, qué no, y por qué.

## 1. ¿Cuánto detalle se gana?

La superresolución por fusión (multi-frame SR) aprovecha dos cosas:

1. **Diversidad de muestreo**: cada frame cae en una fase subpíxel distinta. Al
   registrarlos y fusionarlos se muestrea la escena en una grilla más fina.
2. **Promediado**: el ruido (que es independiente entre frames) baja como √N.
   Con 100 frames, el ruido baja ~10×.

Con eso, la ganancia realista es:

* **hasta 2× lineal** (4× píxeles) sobre la resolución de la fuente;
* reducción de ruido de √N (muy visible en sombras);
* recuperación parcial del desenfoque de la PSF (óptica + apertura del píxel)
  por deconvolución.

Lo que **no** se puede:

* superar el límite de difracción del objetivo;
* inventar detalle que el sensor no muestreó (texto ilegible sigue ilegible);
* congelar movimiento: el objeto debe estar quieto;
* compensar cambios de iluminación/exposición entre frames.

Por eso "10K" se interpreta como deseo de microdetalle, no como una promesa de
resolución. La salida máxima está limitada por `max_output_pixels` (32 MP).

## 2. Por qué el benchmark es honesto

`scripts/benchmark_sr.py` simula una cámara real:

* escena **band-limitada** (una óptica real no entrega contenido por encima de
  su Nyquist; si el ground truth tuviera ruido blanco píxel a píxel, ninguna
  fusión podría recuperarlo y el benchmark sería tramposo);
* PSF óptica de 0.5 px de la fuente (típica de un objetivo de teléfono);
* jitter subpíxel aleatorio, ruido gaussiano σ=3 y JPEG q=88;
* baseline: **bicúbico de un solo frame**, que es lo que hace un visor o una
  cámara normal.

Resultado (GT 1200×900, 120 frames): bicúbico 24.34 dB → SuperFoto 30.81 dB
(**+6.47 dB**, SSIM 0.9214 → 0.9497). En la placa: **+5.31 dB** en una sesión
sintética a escala 2.0.

La fusión *sola* (promedio de frames alineados) queda por debajo del bicúbico:
promediar interpolaciones reduce ruido pero no resuelve el problema inverso del
muestreo. La ganancia aparece con el **refinamiento IBP** (que impone
consistencia con los frames medidos) y la **deconvolución RL** de la PSF. Eso es
lo que reporta la UI y el `session.json`.

### 2.1 Validación en el mundo real

El benchmark es sintético por necesidad (hace falta ground truth), pero el
pipeline también se midió sobre una muestra real de microscopía: un estoma de
hoja filmado 19 s a 1920×1080 y 25 fps, con el teléfono fijo contra el ocular.

| Métrica | Valor |
|---|---|
| Frames vistos / aceptados | 487 / **186** |
| Rechazados | 301 (176 por alineación inválida) |
| Salida | 3840×2160 (2× lineal, 4× píxeles) |
| Tiempo de captura | 459.5 s |
| Postproceso | 68.6 s |
| Shift medio / máximo | 0.66 px / 1.55 px |
| Cobertura 1× / 4× / 16× | 100 % / 100 % / 100 % |

El shift medio de 0.66 px es la prueba de que había diversidad subpíxel real y
de que la escena estaba quieta. La comparativa visual está en
[`img/compare_stoma.jpg`](img/compare_stoma.jpg) (estoma completo) y
[`img/compare_stoma_detail.jpg`](img/compare_stoma_detail.jpg) (detalle 1:1).
A la izquierda, un solo frame ampliado 2× con bicúbico; a la derecha, la fusión
de 186 frames. La mejora se ve en el contorno de los cloroplastos, en la
separación de las células oclusivas y en la reducción del ruido de croma.

### 2.2 El contraejemplo que también importa

El mismo campo, filmado 50 s con la cámara moviéndose, vio **1250 frames** y
solo aceptó **7**, con un shift medio de **10.2 px**. El resultado fue pobre, y
el sistema lo dijo: aviso de escena en movimiento y pocos frames útiles. Eso no
es un fallo del algoritmo; es el algoritmo negándose a inventar una imagen que
los datos no sostienen. Si la escena se mueve, la solución es fijar la cámara y
el objeto, no bajar el umbral de calidad.

## 3. El zoom "microscopio" es digital

La UI permite zoom hasta 8× con rueda, arrastre para pan, y dos descargas:

* **Recorte 1:1 visible**: recorte a resolución nativa de lo que se ve.
* **Recorte central 4×**: 1/4 del área visible, ampliada 4× con vecino más
  cercano (sin interpolar) para inspeccionar píxeles reales.

El zoom > 1× no agrega información: solo acerca lo que la fusión ya recuperó.
La forma correcta de comparar es el recorte 1:1 contra el frame original.

## 4. Cámaras en la UNO Q

`/dev/video0` y `/dev/video1` de la UNO Q son **decodificadores Qualcomm Venus**,
no cámaras. `WebcamSource` los detecta por sysfs (`venus`, `decoder`, `qcom`) y
los descarta antes de abrir el dispositivo; si no hay UVC real, devuelve un
error explicativo que la UI muestra. En el host de desarrollo, los índices 0 y 2
sí son cámaras (HP HD Camera).

## 5. Rendimiento y disco

* El postproceso (IBP + RL) es un costo **fijo** (~25 s a 2 MP, ~36 s a 4.7 MP,
  ~69 s a 8.3 MP) porque trabaja sobre un buffer acotado de 15–32 frames, no
  sobre la sesión completa.
* La captura+alineación+fusión cuesta ~0.43 s/frame a 720p y ~0.60 s/frame a
  1080p en la placa (escala 1.5). Para 9000 frames: ~66–89 min. En la prueba
  real, 487 frames de 1080p a escala 2.0 tardaron 459.5 s (≈0.94 s/frame,
  incluyendo lectura de archivo y descartes).
* Los acumuladores viven en RAM (float32) y se limitan con
  `max_accumulator_bytes` (700 MB) y `max_output_pixels` (32 MP). Si la escala
  pedida no entra, se reduce automáticamente y se informa la escala efectiva.
* En disco solo quedan los resultados (PNG/JPEG/compare/coverage/JSON) y se
  conservan las 3 sesiones más recientes. Las escrituras son atómicas
  (`.tmp` + `os.replace`) y se exige `min_free_mb` (500 MB) libres: si no
  alcanzan, se purgan sesiones viejas y si aún falta se aborta con aviso.
* Si la escena cambia de campo y más de la mitad de los frames no alinean, la
  sesión avisa y fusiona solo el segmento estable (no mezcla campos distintos).

## 6. Convivencia con el resto de la placa

El servicio escucha en el puerto **8010**. El 8000 es de `detector-tarjetas` y
nunca se toca. El despliegue no usa `sudo` (no hay contraseña disponible) y la
unidad systemd se entrega como ejemplo para instalar manualmente.
