// Скетч для демо-схемы wokwi_demo (KiCad → Wokwi).
// Пины как на схеме: кнопка D2 (подтяжка R1 к +5V), серво D6, зуммер D8,
// RGB-светодиод D9/D10/D11, светодиод D13, потенциометр A0.
#include <Servo.h>

const int PIN_BTN = 2;
const int PIN_SERVO = 6;
const int PIN_BUZZER = 8;
const int PIN_RGB_R = 9;
const int PIN_RGB_G = 10;
const int PIN_RGB_B = 11;
const int PIN_LED = 13;
const int PIN_POT = A0;

Servo servo;

// color — 3 бита: R, G, B
void setRgb(int color) {
  digitalWrite(PIN_RGB_R, color & 1);
  digitalWrite(PIN_RGB_G, (color >> 1) & 1);
  digitalWrite(PIN_RGB_B, (color >> 2) & 1);
}

void setup() {
  pinMode(PIN_BTN, INPUT);  // внешняя подтяжка R1: нажатие = LOW
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_RGB_R, OUTPUT);
  pinMode(PIN_RGB_G, OUTPUT);
  pinMode(PIN_RGB_B, OUTPUT);
  servo.attach(PIN_SERVO);
  Serial.begin(115200);
  Serial.println("KiCad -> Wokwi demo");
}

void loop() {
  unsigned long now = millis();
  digitalWrite(PIN_LED, (now / 500) % 2);  // мигает раз в секунду

  int pot = analogRead(PIN_POT);
  servo.write(map(pot, 0, 1023, 0, 180));  // потенциометр поворачивает серво

  if (digitalRead(PIN_BTN) == LOW) {
    tone(PIN_BUZZER, 440 + pot);   // пока кнопка нажата — пищит
    setRgb(1 + (now / 300) % 7);   // и перебирает цвета RGB
  } else {
    noTone(PIN_BUZZER);
    setRgb(0);
  }
  delay(10);
}
