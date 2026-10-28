// 安卓手柄接入：DmActivity 把系统的手柄按键（KeyEvent）与摇杆（MotionEvent）按设备各自整理好，经 nativePad 交到这里，
// 合并后交给宿主 dm_pad_set（与触屏输入在游戏取手柄时合并，hle_hid.c）。规则照 iOS 的 dm_gamepad.m：
//   · 每个设备各存各的；交给游戏的是合起来的——键取或、每根摇杆取推得最远的那个。断开一个只拿掉它那一份。
//   · 手柄「明确输入」（按下任何键，或摇杆推过 25%、回到 15% 内算回中）→ 手柄模式（dm_padmode_request(1)，布局换原版手柄版）；
//     碰屏幕 → 触屏模式（touch_android.c 调 dm_gamepad_touched）。碰过屏幕后摇杆要先回中再推出来才重新接管，按键随时接管。
//   · 触屏模式下摇杆不交给游戏；手柄模式下没推出来的摇杆给 0。
//   · 面键：安卓按位置给 KEYCODE_BUTTON_A = 下、B = 右、X = 左、Y = 上，与 Xbox 手柄一致，所以一律走 Xbox 键位与 Xbox 键帽
//     （dm_padmap_face_xbox，下确认、右返回）；按位置换算在 Java 侧已做成 A/B/X/Y 四个「系统键」。
//   · 不接 iOS 的「ZR 空格 = 免费回城」：安卓前端还没有 dm_front_navigation 的面板观测，ZR 照原版交给游戏（同 tvOS）。
// Android controller input: DmActivity collects the system's controller keys (KeyEvent) and sticks (MotionEvent) per device and passes them here
// through nativePad; merged, they go to the host's dm_pad_set (merged with touch input when the game reads the controller, hle_hid.c). Rules as
// iOS dm_gamepad.m:
//   · Each device is stored separately; the game gets the merge -- buttons ORed, each stick from whichever is pushed furthest. Disconnecting one
//     removes only its share.
//   · Clear controller input (any button pressed, or a stick pushed past 25%; back within 15% counts as centred) -> controller mode
//     (dm_padmode_request(1), the original controller layouts); touching the screen -> touch mode (touch_android.c calls dm_gamepad_touched).
//     After a touch a stick must return to centre and be pushed again to take over; buttons take over at any time.
//   · Sticks are not delivered in touch mode; in controller mode a stick that is not pushed out gives 0.
//   · Face buttons: Android reports by position, KEYCODE_BUTTON_A = bottom, B = right, X = left, Y = top, as on an Xbox controller, so the Xbox
//     layout and Xbox glyphs are always used (dm_padmap_face_xbox: bottom confirms, right goes back); Java already turns the keys into the four
//     "system" buttons A/B/X/Y.
//   · iOS's "ZR on an empty slot = free town portal" is not wired: the Android front end has no dm_front_navigation panel observation yet, so ZR
//     goes to the game unchanged (as on tvOS).
#include <jni.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include "../host/dm.h"
#include "../host/dm_input.h"
#include "../host/dm_padmap.h"

void dm_padmode_request(int pad);   // 宿主 dm_padmode.c：布局换成原版手柄版 / 触屏版 | Host dm_padmode.c: switch the layouts to the controller / touch version

// Java 交来的系统键位（与 DmActivity.PAD_* 一致）：A/B/X/Y 是按位置换算后的四个面键，其余与 dm_input.h 同位
// System buttons from Java (same as DmActivity.PAD_*): A/B/X/Y are the four face buttons after the positional mapping, the rest share dm_input.h's bits
enum { SYS_A = 1 << 0, SYS_B = 1 << 1, SYS_X = 1 << 2, SYS_Y = 1 << 3, SYS_FACE = 15 };

enum { NSLOT = 6 };
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { int used, id; uint64_t b; int32_t ax[4]; } g_s[NSLOT];
static int g_pad_mode, g_need_center, g_on[2];
static uint64_t g_prev_b;

int dm_gamepad_mode(void) { return __atomic_load_n(&g_pad_mode, __ATOMIC_RELAXED); }
static void set_mode(int pad) {
    if (g_pad_mode == pad) return;
    __atomic_store_n(&g_pad_mode, pad, __ATOMIC_RELAXED); dm_padmode_request(pad);
    LOG("手柄：%s", pad ? "手柄有输入 → 手柄模式" : "碰了屏幕 / 手柄断开 → 触屏模式");
}
static int slot_of(int id, int make) {
    int fr = -1;
    for (int i = 0; i < NSLOT; i++) { if (g_s[i].used && g_s[i].id == id) return i; if (fr < 0 && !g_s[i].used) fr = i; }
    if (make && fr >= 0) { memset(&g_s[fr], 0, sizeof g_s[fr]); g_s[fr].used = 1; g_s[fr].id = id; return fr; }
    return -1;
}
static int32_t axis(float v) { if (!(v == v)) v = 0; if (v > 1) v = 1; if (v < -1) v = -1; return (int32_t)lroundf(v * DM_STICK_MAX); }
// 迟滞：推过 25% 算推出，回到 15% 内算回中 | Hysteresis: past 25% counts as pushed out, back within 15% counts as centred
static int stick_on(int was, int32_t x, int32_t y) { double m = hypot(x, y) / DM_STICK_MAX; return was ? m >= 0.15 : m >= 0.25; }
// 把各设备合起来、定模式、交给宿主；调用方持锁 | Merge all devices, decide the mode and hand the result to the host; the caller holds the lock
static void apply_locked(void) {
    uint64_t b = 0; int32_t ax[4] = {0}; double best[2] = {0};
    for (int i = 0; i < NSLOT; i++) { if (!g_s[i].used) continue; b |= g_s[i].b;
        for (int k = 0; k < 2; k++) { double m = hypot(g_s[i].ax[2 * k], g_s[i].ax[2 * k + 1]);
            if (m > best[k]) { best[k] = m; ax[2 * k] = g_s[i].ax[2 * k]; ax[2 * k + 1] = g_s[i].ax[2 * k + 1]; } } }
    g_on[0] = stick_on(g_on[0], ax[0], ax[1]); g_on[1] = stick_on(g_on[1], ax[2], ax[3]);
    if (!g_on[0] && !g_on[1]) g_need_center = 0;   // 碰过屏幕后摇杆回过中了：之后再推出来算数 | The sticks have centred since the touch: pushing out again counts
    if ((b & ~g_prev_b) || ((g_on[0] || g_on[1]) && !g_need_center)) { dm_padmode_request_glyph(DM_GLYPH_XBOX); set_mode(1); }   // 先报键帽再要手柄版 | Report the glyphs before asking for the controller layout
    g_prev_b = b;
    if (!g_pad_mode || !g_on[0]) ax[0] = ax[1] = 0;
    if (!g_pad_mode || !g_on[1]) ax[2] = ax[3] = 0;
    uint64_t game = (b & ~(uint64_t)SYS_FACE) | dm_padmap_face_xbox(!!(b & SYS_A), !!(b & SYS_B), !!(b & SYS_X), !!(b & SYS_Y), 0);
    dm_pad_set(game, ax[0], ax[1], ax[2], ax[3]);
}
// 前端：碰了屏幕。摇杆此刻偏着的话要先回中才重新接管 | Front end: the screen was touched. A deflected stick must centre before taking over again
void dm_gamepad_touched(void) {
    pthread_mutex_lock(&g_lock);
    if (g_pad_mode) { g_need_center = 1; set_mode(0); apply_locked(); }
    pthread_mutex_unlock(&g_lock);
}

JNIEXPORT void JNICALL Java_com_xiaoboluo_d2r_DmActivity_nativePad(JNIEnv *env, jclass cls, jint id, jlong buttons, jfloat lx, jfloat ly, jfloat rx, jfloat ry) {
    (void)env; (void)cls;
    pthread_mutex_lock(&g_lock);
    int i = slot_of(id, 1);
    if (i >= 0) { g_s[i].b = (uint64_t)buttons; g_s[i].ax[0] = axis(lx); g_s[i].ax[1] = axis(ly); g_s[i].ax[2] = axis(rx); g_s[i].ax[3] = axis(ry); apply_locked(); }
    pthread_mutex_unlock(&g_lock);
}
// 设备断开：只拿掉它那一份；一个都不剩才回触屏模式 | Device removed: drop only its share; back to touch mode only when none is left
JNIEXPORT void JNICALL Java_com_xiaoboluo_d2r_DmActivity_nativePadDrop(JNIEnv *env, jclass cls, jint id) {
    (void)env; (void)cls;
    pthread_mutex_lock(&g_lock);
    int i = slot_of(id, 0);
    if (i >= 0) { LOG("手柄：断开（设备 %d）", (int)id); memset(&g_s[i], 0, sizeof g_s[i]); apply_locked();
        int any = 0; for (int k = 0; k < NSLOT; k++) any |= g_s[k].used;
        if (!any) set_mode(0); }
    pthread_mutex_unlock(&g_lock);
}
// 失活 / 进后台：所有设备的键和摇杆清零（按着键切走、在后台松开，回来不会补「松开」）
// Pause / background: zero every device's buttons and sticks (a button held while switching away and released in the background gets no release on return)
JNIEXPORT void JNICALL Java_com_xiaoboluo_d2r_DmActivity_nativePadReset(JNIEnv *env, jclass cls) {
    (void)env; (void)cls;
    pthread_mutex_lock(&g_lock);
    int any = 0;
    for (int i = 0; i < NSLOT; i++) if (g_s[i].used) { any |= g_s[i].b != 0 || g_s[i].ax[0] || g_s[i].ax[1] || g_s[i].ax[2] || g_s[i].ax[3]; g_s[i].b = 0; memset(g_s[i].ax, 0, sizeof g_s[i].ax); }
    if (any) { LOG("手柄：失活，手柄的键和摇杆清零"); apply_locked(); }
    pthread_mutex_unlock(&g_lock);
}
