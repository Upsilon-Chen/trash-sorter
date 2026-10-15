/*
 * ============================================================================
 *  YOLO 垃圾自动识别分类系统 —— ESP32 双舵机四向分类固件
 * ============================================================================
 *
 *  硬件：两个舵机组成二维云台，靠"水平 + 竖直"两组角度组合出 4 个倾倒方向。
 *        角度范围与脉宽标定沿用实测通过的 sketch_sep26b.ino：
 *            水平舵机（引脚 16）：0~270°   -> 500~2500us
 *            竖直舵机（引脚 17）：30~180°  -> 500~2400us（低于 30° 会被夹住）
 *        四个方向（slot 0~3，与 backend/config/classes.json 的 groups 对应）：
 *            slot 0: 可回收物（paper / plastic）      H=30  V=45
 *            slot 1: 有害垃圾（battery / medicine）   H=120 V=145
 *            slot 2: 厨余垃圾（peel）                 H=30  V=145
 *            slot 3: 其他垃圾（mask / tableware）     H=120 V=45
 *
 *  串口：115200 8N1，一行一个 JSON（'\n' 结尾）
 *
 *    PC -> ESP32
 *        {"cmd":"sort","class":"battery","slot":1,"angle":90}      按 slot 选方向
 *        {"cmd":"sort","slot":1,"h":210,"v":170}                   可选：显式覆盖 H/V
 *        {"cmd":"ping"}                                            心跳
 *        {"cmd":"status"}                                          主动查询
 *
 *    ESP32 -> PC
 *        {"status":"ready"}                    启动就绪 / 查询应答
 *        {"status":"done","slot":0}            已完成倾倒并回到待机
 *        {"status":"busy","slot":0}            上一次动作未结束，本次忽略
 *        {"status":"error","msg":"..."}        指令非法
 *
 *  动作时序（两个轴分时动作，竖直必须等水平到位后再倾斜）：
 *      水平转向 -> 停 H_TRAVEL_MS -> 竖直倾斜 -> 停 V_TRAVEL_MS
 *      -> 保持倾倒 DUMP_HOLD_MS -> 竖直回垂直 -> 停 V_TRAVEL_MS + RETURN_SETTLE_MS
 *      -> 回复 done
 *      水平舵机倒完不回中立位，就停在当前倾倒角（下一条指令会重新定位），
 *      竖直必须放平，托盘才能接下一件。整个循环 CYCLE_MS = 3000 ms。
 *
 *  注意 1：协议里的 angle 字段 PC 端仍会下发（来自 classes.json），但本固件不使用，
 *          4 个方向的角度由下面的 DUMP_H / DUMP_V 表决定；需要临时改角度用 h/v 覆盖。
 *  注意 2：串口同时是协议通道，默认不向 Serial 打印任何日志（见 DBG 宏），
 *          以免文本混进 JSON 行里。
 *  注意 3：两个舵机不要从 ESP32 的 3.3V/5V 引脚取电，请用独立 5V 供电并与板子共地。
 * ============================================================================
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ============================================================================
//  用户配置区：按实际接线 / 舵机型号修改
// ============================================================================

// 舵机信号引脚
constexpr uint8_t PIN_HORIZONTAL = 16;  // 水平舵机
constexpr uint8_t PIN_VERTICAL = 17;    // 竖直舵机

// 待机（回位）角度。竖直 95° = 托盘与地面垂直（水平状态）；水平 180° 是中立位。
constexpr int STANDBY_H = 180;
constexpr int STANDBY_V = 100;

// 4 个倾倒方向的角度表：数组下标即协议里的 slot。
//   水平：只用两个相差 180° 的角度（30 / 210），把平台对到左右两档；
//   竖直：以 95°（垂直）为基准，向两侧各倾 50°（45 / 145）；
//   两者组合就是把托盘分别压向四个互相垂直的方向：
//       slot 0 = (30, 45)    slot 1 = (210, 145)
//       slot 2 = (30, 145)   slot 3 = (210, 45)
//   竖直倾角按"离垂直基准等幅"取，保证四个方向倒得一样干净；
//   若某个方向倒不干净就把该侧的 V 再远离 95°（如 38 / 152），反之则靠近。
constexpr int DUMP_H[] = {30, 120, 30, 120};
constexpr int DUMP_V[] = {45, 145, 145, 45};

// 机械行程（越界会被夹到边界）
constexpr int H_ANGLE_MIN = 0;
constexpr int H_ANGLE_MAX = 270;
constexpr int V_ANGLE_MIN = 30;
constexpr int V_ANGLE_MAX = 180;

// 脉宽标定：水平 0~270° 对应 500~2500us；竖直 0~180° 对应 500~2400us
constexpr int H_PULSE_MIN_US = 500;
constexpr int H_PULSE_MAX_US = 2500;
constexpr int V_PULSE_MIN_US = 500;
constexpr int V_PULSE_MAX_US = 2400;

// PWM 周期（Hz）：模拟舵机标准 50Hz
constexpr int SERVO_PWM_HZ = 50;

// 动作分段延时（ms）。两个轴必须分时动作：
//   竖直舵机一定要等水平舵机转到位后再倾斜 —— 同时动会互相抢扭矩，
//   而且托盘还没对准桶就把垃圾倒下去。
// 完整时序（水平倒完不回中立位，只把竖直放平）：
//   水平转向 -> 等 H_TRAVEL_MS -> 竖直倾斜 -> 等 V_TRAVEL_MS
//   -> 保持倾倒 DUMP_HOLD_MS -> 竖直回垂直 -> 等 V_TRAVEL_MS + RETURN_SETTLE_MS -> 回复 done
constexpr uint32_t H_TRAVEL_MS = 700;       // 水平最大 150° 行程（180°->30°），按 ~0.2s/60° 留足余量
constexpr uint32_t V_TRAVEL_MS = 400;       // 竖直每次只走 50°
constexpr uint32_t DUMP_HOLD_MS = 1200;     // 保持倾倒，让垃圾滑出去
constexpr uint32_t RETURN_SETTLE_MS = 300;  // 竖直放平后的稳定时间，之后才回复 done

// 一次完整动作的总时长：3000 ms。PC 端的同类冷却时间必须大于它，
// 否则会在 ESP32 还忙的时候下发指令，被 busy 丢掉（见 backend/config/classes.json）。
constexpr uint32_t CYCLE_MS = H_TRAVEL_MS + V_TRAVEL_MS + DUMP_HOLD_MS + V_TRAVEL_MS + RETURN_SETTLE_MS;
static_assert(CYCLE_MS <= 5000, "单次动作时间过长，请同步调大 classes.json 里的冷却时间");

// 串口波特率
constexpr uint32_t SERIAL_BAUD = 115200;

// 单行最大字节数，超长直接丢弃，避免内存被异常数据撑爆
constexpr size_t MAX_LINE_LENGTH = 256;

// 空闲时周期性重发 ready 的间隔（ms），0 表示关闭。
// 若 PC 端在 ESP32 启动之后才打开串口，可把它设成 5000 之类。
// 这里用宏而不是 constexpr，是为了让下面的 #if 能在编译期生效；
// 也可以在 platformio.ini 里用 -DREADY_REPEAT_MS=5000 覆盖。
#ifndef READY_REPEAT_MS
#define READY_REPEAT_MS 0
#endif

// 打开后在串口打印调试日志。会与 JSON 协议共用串口，
// PC 端会把非 JSON 行当文本日志显示（不影响协议解析）。
#ifndef TRASH_SORTER_DEBUG
#define TRASH_SORTER_DEBUG 0
#endif

// ============================================================================

constexpr size_t SLOT_COUNT = sizeof(DUMP_H) / sizeof(DUMP_H[0]);
static_assert(SLOT_COUNT >= 1 && SLOT_COUNT <= 8, "DUMP_H 的槽位数量应在 1..8 之间");
static_assert(sizeof(DUMP_H) == sizeof(DUMP_V), "DUMP_H 与 DUMP_V 的长度必须一致");

#if TRASH_SORTER_DEBUG
#define DBG(...) Serial.printf(__VA_ARGS__)
#else
#define DBG(...) \
  do {           \
  } while (0)
#endif

// 一次分类动作的阶段（两个舵机分时动作；水平倒完停在原位，所以没有"回中"阶段）
enum class Phase : uint8_t { None, MoveH, Tilt, Dump, Level };

// 当前动作（舵机是独占资源，同一时刻只允许一个）
struct SortJob {
  bool active = false;
  Phase phase = Phase::None;
  uint8_t slot = 0;
  int h = STANDBY_H;
  int v = STANDBY_V;
  uint32_t phaseStartedAt = 0;  // millis()
};

static Servo g_horizontal;
static Servo g_vertical;
static SortJob g_job;
static char g_lineBuffer[MAX_LINE_LENGTH + 1];
static size_t g_lineLength = 0;
static bool g_lineOverflow = false;
#if READY_REPEAT_MS > 0
static uint32_t g_lastReadyAt = 0;
#endif

// ---------------------------------------------------------------------------
//  角度 -> 脉宽（与 sketch_sep26b.ino 的标定一致）
// ---------------------------------------------------------------------------

static int clampInt(int value, int low, int high) {
  if (value < low) return low;
  if (value > high) return high;
  return value;
}

static int hPulse(int angle) {
  angle = clampInt(angle, H_ANGLE_MIN, H_ANGLE_MAX);
  return H_PULSE_MIN_US + static_cast<int>((angle / 270.0) * (H_PULSE_MAX_US - H_PULSE_MIN_US));
}

static int vPulse(int angle) {
  angle = clampInt(angle, V_ANGLE_MIN, V_ANGLE_MAX);
  return V_PULSE_MIN_US + static_cast<int>((angle / 180.0) * (V_PULSE_MAX_US - V_PULSE_MIN_US));
}

static void setHorizontal(int angle) { g_horizontal.writeMicroseconds(hPulse(angle)); }

static void setVertical(int angle) { g_vertical.writeMicroseconds(vPulse(angle)); }

// 两个轴分开控制：倾倒时先水平、后竖直；回位时先竖直、后水平
static void setStandbyHorizontal() { setHorizontal(STANDBY_H); }

static void setStandbyVertical() { setVertical(STANDBY_V); }

// 仅在 setup() 里用：上电同时摆到待机位
static void goToStandby() {
  setStandbyHorizontal();
  setStandbyVertical();
}

// ---------------------------------------------------------------------------
//  串口发送
// ---------------------------------------------------------------------------

static void sendJson(JsonDocument &doc) {
  serializeJson(doc, Serial);
  Serial.print('\n');  // 一行一条，PC 端按行解析
}

static void sendStatus(const char *status) {
  JsonDocument doc;
  doc["status"] = status;
  sendJson(doc);
}

static void sendDone(uint8_t slot) {
  JsonDocument doc;
  doc["status"] = "done";
  doc["slot"] = slot;
  sendJson(doc);
}

static void sendBusy(uint8_t slot) {
  JsonDocument doc;
  doc["status"] = "busy";
  doc["slot"] = slot;
  sendJson(doc);
}

static void sendError(const char *message) {
  JsonDocument doc;
  doc["status"] = "error";
  doc["msg"] = message;
  sendJson(doc);
  DBG("[err] %s\n", message);
}

// ---------------------------------------------------------------------------
//  动作调度
// ---------------------------------------------------------------------------

static void startJob(uint8_t slot, int h, int v) {
  g_job.active = true;
  g_job.phase = Phase::MoveH;
  g_job.slot = slot;
  g_job.h = h;
  g_job.v = v;
  g_job.phaseStartedAt = millis();

  // 第一步只转水平，竖直暂时保持在 95°（垂直），等水平到位再倾斜
  setStandbyVertical();
  setHorizontal(h);
  DBG("[job] slot=%u 水平 -> %d(%dus)，竖直待命 %d\n", static_cast<unsigned>(slot), h, hPulse(h), STANDBY_V);
}

static void updateJob() {
  if (!g_job.active) {
    return;
  }
  const uint32_t now = millis();
  const uint32_t elapsed = now - g_job.phaseStartedAt;

  switch (g_job.phase) {
    case Phase::MoveH:  // 等水平舵机转到位
      if (elapsed >= H_TRAVEL_MS) {
        setVertical(g_job.v);
        g_job.phase = Phase::Tilt;
        g_job.phaseStartedAt = now;
        DBG("[job] slot=%u 竖直倾斜 -> %d(%dus)\n", static_cast<unsigned>(g_job.slot), g_job.v,
            vPulse(g_job.v));
      }
      break;

    case Phase::Tilt:  // 等竖直倾到位
      if (elapsed >= V_TRAVEL_MS) {
        g_job.phase = Phase::Dump;
        g_job.phaseStartedAt = now;
      }
      break;

    case Phase::Dump:  // 保持倾倒，让垃圾滑出去
      if (elapsed >= DUMP_HOLD_MS) {
        setStandbyVertical();
        g_job.phase = Phase::Level;
        g_job.phaseStartedAt = now;
        DBG("[job] slot=%u 竖直回位\n", static_cast<unsigned>(g_job.slot));
      }
      break;

    case Phase::Level:  // 等竖直放平并稳定，之后回复 done（水平保持在倾倒位不动）
      if (elapsed >= V_TRAVEL_MS + RETURN_SETTLE_MS) {
        const uint8_t finishedSlot = g_job.slot;
        g_job.active = false;
        g_job.phase = Phase::None;
        sendDone(finishedSlot);
        DBG("[job] slot=%u done（水平停在 %d，竖直 %d）\n", static_cast<unsigned>(finishedSlot), g_job.h,
            STANDBY_V);
      }
      break;

    case Phase::None:
    default:
      break;
  }
}

// ---------------------------------------------------------------------------
//  指令处理
// ---------------------------------------------------------------------------

static void handleSort(JsonDocument &doc) {
  if (g_job.active) {
    sendBusy(g_job.slot);
    return;
  }

  if (!doc["slot"].is<int>()) {
    sendError("missing slot");
    return;
  }
  const int slot = doc["slot"].as<int>();
  if (slot < 0 || slot >= static_cast<int>(SLOT_COUNT)) {
    sendError("slot out of range");
    return;
  }

  // 默认取 slot 对应的方向；可用 h / v 显式覆盖（标定或手动微调用）
  int h = DUMP_H[slot];
  int v = DUMP_V[slot];
  if (doc["h"].is<int>()) h = doc["h"].as<int>();
  if (doc["v"].is<int>()) v = doc["v"].as<int>();

  const int rawH = h;
  const int rawV = v;
  h = clampInt(h, H_ANGLE_MIN, H_ANGLE_MAX);
  v = clampInt(v, V_ANGLE_MIN, V_ANGLE_MAX);
  if (h != rawH || v != rawV) {
    DBG("[warn] H=%d V=%d 越界，夹到 H=%d V=%d\n", rawH, rawV, h, v);
  }

  const char *className = doc["class"].is<const char *>() ? doc["class"].as<const char *>() : "";
  (void)className;  // 仅调试日志使用，关闭 DEBUG 时避免未使用告警
  // angle 字段由 PC 端下发但本固件不使用（4 个方向由 DUMP_H/DUMP_V 决定）
  DBG("[cmd] sort class=%s slot=%d angle=%d -> H=%d V=%d%s\n", className, slot,
      doc["angle"].is<int>() ? doc["angle"].as<int>() : -1, h, v,
      (doc["h"].is<int>() || doc["v"].is<int>()) ? " (override)" : "");

  startJob(static_cast<uint8_t>(slot), h, v);
}

static void handleLine(const char *line) {
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, line);
  if (error) {
    sendError("invalid json");
    return;
  }
  if (!doc.is<JsonObject>()) {
    sendError("expect json object");
    return;
  }

  const char *cmd = doc["cmd"].is<const char *>() ? doc["cmd"].as<const char *>() : "";
  if (strcmp(cmd, "sort") == 0) {
    handleSort(doc);
  } else if (strcmp(cmd, "ping") == 0) {
    sendStatus("pong");
  } else if (strcmp(cmd, "status") == 0) {
    sendStatus("ready");
  } else {
    sendError("unknown cmd");
  }
}

// ---------------------------------------------------------------------------
//  串口接收：按行缓冲，'\n' 触发解析
// ---------------------------------------------------------------------------

static void readSerial() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());

    if (c == '\n') {
      if (!g_lineOverflow && g_lineLength > 0) {
        g_lineBuffer[g_lineLength] = '\0';
        handleLine(g_lineBuffer);
      } else if (g_lineOverflow) {
        DBG("[warn] 单行超过 %u 字节，已丢弃\n", static_cast<unsigned>(MAX_LINE_LENGTH));
      }
      g_lineLength = 0;
      g_lineOverflow = false;
      continue;
    }

    if (c == '\r') {
      continue;  // 兼容 CRLF
    }

    if (g_lineOverflow) {
      continue;  // 已溢出，丢弃到本行结束
    }
    if (g_lineLength < MAX_LINE_LENGTH) {
      g_lineBuffer[g_lineLength++] = c;
    } else {
      g_lineOverflow = true;
    }
  }
}

// ---------------------------------------------------------------------------
//  setup / loop
// ---------------------------------------------------------------------------

void setup() {
  Serial.begin(SERIAL_BAUD);
  Serial.setTimeout(50);

  g_horizontal.setPeriodHertz(SERVO_PWM_HZ);
  g_vertical.setPeriodHertz(SERVO_PWM_HZ);
  g_horizontal.attach(PIN_HORIZONTAL);
  g_vertical.attach(PIN_VERTICAL);
  DBG("[init] H -> GPIO %u, V -> GPIO %u\n", static_cast<unsigned>(PIN_HORIZONTAL),
      static_cast<unsigned>(PIN_VERTICAL));

  // 上电先进入待机，避免上电抖动
  goToStandby();
  delay(500);

  g_lineLength = 0;
  g_lineOverflow = false;
#if READY_REPEAT_MS > 0
  g_lastReadyAt = millis();
#endif

  sendStatus("ready");  // 启动就绪
  DBG("[init] ready, slots=%u\n", static_cast<unsigned>(SLOT_COUNT));
}

void loop() {
  readSerial();
  updateJob();

#if READY_REPEAT_MS > 0
  // 空闲时周期性重发 ready，方便 PC 端在 ESP32 启动之后才打开串口的情况
  if (!g_job.active) {
    const uint32_t now = millis();
    if (now - g_lastReadyAt >= READY_REPEAT_MS) {
      sendStatus("ready");
      g_lastReadyAt = now;
    }
  }
#endif
}
