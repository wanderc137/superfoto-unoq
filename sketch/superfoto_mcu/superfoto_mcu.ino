// superfoto_mcu.ino - indicador de estado en la matriz LED 13x8 de la UNO Q.
//
// Estados: IDLE (respiracion lenta), CAPTURING (onda rapida),
//          PROCESSING (spinner de 8 posiciones), DONE (check + pulso).
// Heartbeat: un pixel parpadea cada 500 ms en todos los estados.
//
// Comandos por Serial (opcionales): 'I' idle, 'C' capturando, 'P' procesando,
// 'D' listo. Sin comandos, cicla solo cada 7 s (modo demo).
//
// No depende del lado Linux: si el upload falla, la app Linux sigue andando.
// Solo usa Arduino_LED_Matrix (API raw: setGrayscaleBits + draw), sin
// ArduinoGraphics (no instalada en la placa).

#include <Arduino.h>
#include "Arduino_LED_Matrix.h"

Arduino_LED_Matrix matrix;

static const uint8_t MATRIX_W = 13;
static const uint8_t MATRIX_H = 8;
static const uint8_t GRAY_BITS = 3;          // valores 0..7
static const uint8_t GRAY_MAX = (1 << GRAY_BITS) - 1;

static uint8_t canvas[MATRIX_H * MATRIX_W];

enum Mode : uint8_t { MODE_IDLE = 0, MODE_CAPTURE, MODE_PROCESS, MODE_DONE };

static Mode mode = MODE_IDLE;
static uint32_t lastCommandMs = 0;
static uint32_t lastModeChangeMs = 0;
static bool autoDemo = true;
static uint32_t framesDrawn = 0;

// --------------------------------------------------------------------------
// Utilidades de dibujo
// --------------------------------------------------------------------------
static inline void clearCanvas() {
  memset(canvas, 0, sizeof(canvas));
}

static inline void setPx(int x, int y, uint8_t value) {
  if (x < 0 || y < 0 || x >= (int)MATRIX_W || y >= (int)MATRIX_H) {
    return;
  }
  canvas[y * MATRIX_W + x] = (value > GRAY_MAX) ? GRAY_MAX : value;
}

static inline uint8_t breathe(uint32_t ms, uint32_t period, uint8_t lo, uint8_t hi) {
  // Onda triangular suave (evita sinf y mantiene el sketch chico).
  uint32_t half = period / 2;
  uint32_t t = ms % period;
  uint32_t phase = (t < half) ? t : (period - t);
  return (uint8_t)(lo + (hi - lo) * phase / (half ? half : 1));
}

static void heartbeat(uint32_t ms) {
  // Pixel de esquina: pulso cada 500 ms, presente en todos los estados.
  setPx(MATRIX_W - 1, 0, ((ms / 500) % 2 == 0) ? GRAY_MAX : 1);
}

// --------------------------------------------------------------------------
// Patrones por estado
// --------------------------------------------------------------------------
static void renderIdle(uint32_t ms) {
  // Barra horizontal que "respira" (2 filas), mas marco tenue.
  uint8_t level = breathe(ms, 2600, 2, GRAY_MAX);
  int half = 2 + (level > 4 ? 2 : (level > 2 ? 1 : 0));
  for (int y = 3; y <= 4; y++) {
    for (int x = MATRIX_W / 2 - half; x <= MATRIX_W / 2 + half; x++) {
      setPx(x, y, level);
    }
  }
  setPx(0, 0, 1); setPx(0, MATRIX_H - 1, 1);
  setPx(MATRIX_W - 1, MATRIX_H - 1, 1);
}

static void renderCapture(uint32_t ms) {
  // Onda rapida de izquierda a derecha con estela.
  int head = (int)((ms / 45) % (MATRIX_W + 6)) - 3;
  for (int k = 0; k < 4; k++) {
    int x = head - k;
    uint8_t level = (k == 0) ? GRAY_MAX : (uint8_t)(GRAY_MAX - k * 2);
    for (int y = 2; y <= 5; y++) {
      setPx(x, y, level);
    }
  }
  // Filas superior/inferior tenues: da sensacion de barrido.
  for (int x = 0; x < (int)MATRIX_W; x++) {
    if (x <= head) {
      setPx(x, 1, 1);
      setPx(x, 6, 1);
    }
  }
}

static void renderProcess(uint32_t ms) {
  // Spinner: 8 posiciones por el perimetro + centro pulsante.
  static const int8_t ring[8][2] = {
    {0, 0}, {3, 0}, {6, 0}, {6, 3}, {6, 7}, {3, 7}, {0, 7}, {0, 3},
  };
  int idx = (int)((ms / 110) % 8);
  for (int k = 0; k < 4; k++) {
    int p = (idx - k + 8) % 8;
    uint8_t level = (k == 0) ? GRAY_MAX : (uint8_t)(GRAY_MAX - k * 2);
    // Cada posicion del anillo se expande a un bloque 2x1 para verse mejor.
    setPx(ring[p][0], ring[p][1], level);
    setPx(ring[p][0] + 1, ring[p][1], level);
  }
  uint8_t center = breathe(ms, 900, 2, GRAY_MAX);
  setPx(3, 5, center);
  setPx(6, 2, center);
}

static void renderDone(uint32_t ms) {
  // Check estatico + pulso de brillo.
  static const uint8_t check[MATRIX_H][MATRIX_W] = {
    {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
    {0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0},
    {0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0},
    {0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 1, 0, 0},
    {0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0},
    {0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0},
    {0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0},
    {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
  };
  uint8_t level = breathe(ms, 1400, 4, GRAY_MAX);
  for (uint8_t y = 0; y < MATRIX_H; y++) {
    for (uint8_t x = 0; x < MATRIX_W; x++) {
      if (check[y][x]) {
        setPx(x, y, level);
      }
    }
  }
}

// --------------------------------------------------------------------------
static void render(uint32_t ms) {
  clearCanvas();
  switch (mode) {
    case MODE_IDLE:    renderIdle(ms); break;
    case MODE_CAPTURE: renderCapture(ms); break;
    case MODE_PROCESS: renderProcess(ms); break;
    case MODE_DONE:    renderDone(ms); break;
  }
  heartbeat(ms);
}

static const char *modeName(Mode m) {
  switch (m) {
    case MODE_IDLE:    return "IDLE";
    case MODE_CAPTURE: return "CAPTURING";
    case MODE_PROCESS: return "PROCESSING";
    case MODE_DONE:    return "DONE";
  }
  return "?";
}

static void setMode(Mode m, uint32_t now) {
  if (m == mode) {
    return;
  }
  mode = m;
  lastModeChangeMs = now;
  Serial.print("[superfoto] estado: ");
  Serial.println(modeName(m));
}

static void handleSerial(uint32_t now) {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) {
      break;
    }
    char cmd = (char)toupper(c);
    switch (cmd) {
      case 'I': autoDemo = false; lastCommandMs = now; setMode(MODE_IDLE, now); break;
      case 'C': autoDemo = false; lastCommandMs = now; setMode(MODE_CAPTURE, now); break;
      case 'P': autoDemo = false; lastCommandMs = now; setMode(MODE_PROCESS, now); break;
      case 'D': autoDemo = false; lastCommandMs = now; setMode(MODE_DONE, now); break;
      case 'A': autoDemo = true; lastCommandMs = now; break;
      default: break;
    }
  }
  // Si no hay comandos hace 20 s, vuelve al ciclo demo.
  if (!autoDemo && (now - lastCommandMs) > 20000UL) {
    autoDemo = true;
  }
}

// --------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  matrix.begin();
  matrix.setGrayscaleBits(GRAY_BITS);
  clearCanvas();
  matrix.draw(canvas);

  // Secuencia de arranque: llena la matriz de izquierda a derecha.
  for (int x = 0; x < (int)MATRIX_W; x++) {
    for (int y = 0; y < (int)MATRIX_H; y++) {
      setPx(x, y, GRAY_MAX);
    }
    matrix.draw(canvas);
    delay(25);
  }
  delay(120);
  clearCanvas();
  matrix.draw(canvas);

  lastModeChangeMs = millis();
  Serial.println("[superfoto] MCU listo: matriz 13x8, estados I/C/P/D por Serial");
}

void loop() {
  uint32_t now = millis();
  handleSerial(now);

  if (autoDemo && (now - lastModeChangeMs) > 7000UL) {
    Mode next = (Mode)((mode + 1) % 4);
    setMode(next, now);
  }

  render(now);
  matrix.draw(canvas);
  framesDrawn++;
  delay(33);  // ~30 fps: suficiente para las animaciones
}
