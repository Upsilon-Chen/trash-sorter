#include <ESP32Servo.h>

// 舵机对象
Servo horizontalServo;  // 引脚 16
Servo verticalServo;    // 引脚 17

// 待机角度
const int STANDBY_H = 180;
const int STANDBY_V = 100;

// 四个状态对应的角度
// 索引 0~3 分别对应按键 '1'~'4'
const int dumpH[4] = {30, 210, 30, 210};   // 水平角度：只有 30 和 210
const int dumpV[4] = {50, 170, 170, 50};   // 竖直角度：1/3 与 2/4 互为反向

// 水平舵机脉宽计算：0~270° 对应 500~2500us
int hPulse(int angle) {
  if (angle < 0) angle = 0;
  if (angle > 270) angle = 270;
  return 500 + (int)((angle / 270.0) * 2000.0);
}

// 竖直舵机脉宽计算：30~180° 有效，对应 500~2400us
int vPulse(int angle) {
  if (angle < 30) angle = 30;
  if (angle > 180) angle = 180;
  return 500 + (int)((angle / 180.0) * 1900.0);
}

// 设置水平角度
void setHorizontal(int angle) {
  horizontalServo.writeMicroseconds(hPulse(angle));
}

// 设置竖直角度
void setVertical(int angle) {
  verticalServo.writeMicroseconds(vPulse(angle));
}

// 回到待机状态
void goToStandby() {
  setHorizontal(STANDBY_H);
  setVertical(STANDBY_V);
  delay(1000);
}

// 执行倾倒动作
void dumpAt(int index) {
  int h = dumpH[index];
  int v = dumpV[index];

  Serial.print("Dump -> H:");
  Serial.print(h);
  Serial.print(" V:");
  Serial.println(v);

  // 1. 转到对应倾倒角度
  setHorizontal(h);
  setVertical(v);
  delay(1500);  // 保持倾倒，让物品倒出

  // 2. 回到待机
  goToStandby();
}

void setup() {
  Serial.begin(115200);
  delay(500);

  horizontalServo.setPeriodHertz(50);
  verticalServo.setPeriodHertz(50);

  horizontalServo.attach(16);
  verticalServo.attach(17);

  // 上电进入待机
  goToStandby();

  Serial.println("Ready. Send '1'~'4' to dump.");
}

void loop() {
  if (Serial.available()) {
    char c = Serial.read();
    if (c >= '1' && c <= '4') {
      int idx = c - '1';
      Serial.print("Received: ");
      Serial.println(c);

      dumpAt(idx);

      Serial.println("Done. Back to standby.");
    }
  }
}