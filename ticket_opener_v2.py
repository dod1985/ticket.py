"""
ticket_opener_v2.py
Pydroid3向け 高精度チケット先着購入ブラウザ起動ツール

方針:
  - 対象URLへの事前アクセスはしない
  - ウォームアップは about:blank のみ
  - 指定時刻に初めて対象URLを webbrowser.open に渡す

使い方:
  python ticket_opener_v2.py
"""

import os
import socket
import struct
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta


# ========================================================
# 設定
# ========================================================

LOG_DIR = "/storage/emulated/0/000STRAGE/ticket_opener"
LOG_FILE = os.path.join(LOG_DIR, "access_log.txt")

NTP_SERVERS = [
    "ntp.nict.jp",
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
]

NTP_SAMPLES_PER_SERVER = 5
NTP_TIMEOUT_SEC = 1.2
NTP_SAMPLE_INTERVAL_SEC = 0.12

BUSY_WAIT_WINDOW_SEC = 0.015
WARMUP_MIN_REMAINING_SEC = 3.0

DEFAULT_DISPATCH_OFFSET_MS = 0.0
MAX_ABS_DISPATCH_OFFSET_MS = 5000.0


# ========================================================
# NTP 時刻補正
# ========================================================

def get_ntp_sample(server, timeout=NTP_TIMEOUT_SEC):
    """NTPオフセット秒とRTT秒を取得する。失敗時は None。"""
    ntp_epoch_delta = 2208988800
    packet = b"\x1b" + b"\x00" * 47

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            t_send = time.time()
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(1024)
            t_recv = time.time()
    except Exception:
        return None

    if len(data) < 48:
        return None

    tx_sec = struct.unpack("!I", data[40:44])[0] - ntp_epoch_delta
    tx_frac = struct.unpack("!I", data[44:48])[0] / 2 ** 32
    server_time = tx_sec + tx_frac
    local_midpoint = (t_send + t_recv) / 2

    return {
        "server": server,
        "offset": server_time - local_midpoint,
        "rtt": t_recv - t_send,
    }


def sync_ntp():
    """複数回NTPを取り、RTTが最小のサンプルを採用する。"""
    print("[NTP] 時刻同期を開始します...")

    for server in NTP_SERVERS:
        print("  ->", server, "に問い合わせ中...")
        samples = []

        for index in range(NTP_SAMPLES_PER_SERVER):
            sample = get_ntp_sample(server)
            label = str(index + 1) + "/" + str(NTP_SAMPLES_PER_SERVER)

            if sample is None:
                print("    ", label + ":", "失敗")
            else:
                samples.append(sample)
                offset_ms = sample["offset"] * 1000
                rtt_ms = sample["rtt"] * 1000
                print(
                    "    ",
                    label + ":",
                    "offset=" + format_ms(offset_ms),
                    "RTT=" + "{:.3f} ms".format(rtt_ms),
                )

            time.sleep(NTP_SAMPLE_INTERVAL_SEC)

        if samples:
            best = min(samples, key=lambda item: item["rtt"])
            print("[NTP] 採用:", best["server"])
            print("      補正:", format_ms(best["offset"] * 1000))
            print("      RTT :", "{:.3f} ms".format(best["rtt"] * 1000))
            return best

    print("[NTP] 取得失敗。補正なしで続行します。")
    return {
        "server": "none",
        "offset": 0.0,
        "rtt": 0.0,
    }


def now_corrected(offset):
    return datetime.fromtimestamp(time.time() + offset)


def format_ms(value):
    return "{:+.3f} ms".format(value)


# ========================================================
# ブラウザ起動
# ========================================================

def open_url(url):
    """Pydroid3で安定しやすい webbrowser.open を使う。"""
    try:
        opened = webbrowser.open(url)
    except Exception as exc:
        print("[ブラウザ] webbrowser.open 失敗:", exc)
        return "failed"

    if opened:
        print("[ブラウザ] webbrowser.open でURLを渡しました")
        return "webbrowser.open"

    print("[ブラウザ] webbrowser.open が False を返しました")
    return "webbrowser.open_false"


def warm_up():
    """対象URLではなく about:blank だけを開いてブラウザを起こす。"""
    print("[ウォームアップ] about:blank でブラウザを起動します")
    try:
        webbrowser.open("about:blank")
        time.sleep(1.5)
        print("[ウォームアップ] 完了")
    except Exception as exc:
        print("[ウォームアップ] 失敗:", exc)


# ========================================================
# スリープ抑制
# ========================================================

def acquire_termux_wake_lock():
    """Termux環境ならwake lockを試す。Pydroid3では失敗してもよい。"""
    try:
        result = subprocess.run(
            ["termux-wake-lock"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False

    if result.returncode == 0:
        print("[wake lock] termux-wake-lock を取得しました")
        return True

    return False


def release_termux_wake_lock():
    try:
        subprocess.run(
            ["termux-wake-unlock"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        pass


def keep_awake_loop(stop_event):
    while not stop_event.is_set():
        time.sleep(0.2)


# ========================================================
# ログ
# ========================================================

def write_log(url, fired_at, target_at, dispatch_at, ntp, offset_ms, method):
    target_diff = (fired_at - target_at).total_seconds() * 1000
    dispatch_diff = (fired_at - dispatch_at).total_seconds() * 1000

    line = (
        "[" + fired_at.strftime("%Y-%m-%d %H:%M:%S.%f") + "] "
        + "URL=" + url + " | "
        + "予定=" + target_at.strftime("%Y-%m-%d %H:%M:%S.%f") + " | "
        + "URL投入予定=" + dispatch_at.strftime("%H:%M:%S.%f") + " | "
        + "販売予定との差=" + format_ms(target_diff) + " | "
        + "URL投入誤差=" + format_ms(dispatch_diff) + " | "
        + "投入オフセット=" + format_ms(offset_ms) + " | "
        + "NTP=" + ntp["server"] + " | "
        + "NTP補正=" + format_ms(ntp["offset"] * 1000) + " | "
        + "NTP_RTT=" + format_ms(ntp["rtt"] * 1000) + " | "
        + "ブラウザ=" + method
        + "\n"
    )

    try:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(LOG_FILE, "a", encoding="utf-8") as file:
            file.write(line)
        print("[ログ]", line.strip())
    except Exception as exc:
        print("[ログ] 保存失敗:", exc)
        print("[ログ] 内容:", line.strip())


# ========================================================
# 入力
# ========================================================

def parse_time_input(raw):
    """
    1から6桁の数字を時刻に変換する。
    1000   -> 10:00:00
    123456 -> 12:34:56
    12     -> 12:00:00
    """
    raw = raw.strip().replace(":", "")
    if not raw.isdigit():
        raise ValueError("数字以外が含まれています")

    raw = raw.ljust(6, "0")[:6]
    hh = int(raw[0:2])
    mm = int(raw[2:4])
    ss = int(raw[4:6])

    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("時刻の範囲外です")

    return hh, mm, ss


def parse_dispatch_offset_ms(raw):
    raw = raw.strip()
    if raw == "":
        return DEFAULT_DISPATCH_OFFSET_MS

    try:
        value = float(raw)
    except ValueError:
        raise ValueError("数値で入力してください")

    if abs(value) > MAX_ABS_DISPATCH_OFFSET_MS:
        raise ValueError("絶対値は5000ms以下にしてください")

    return value


def build_target_datetime(hh, mm, ss, corrected_now):
    target_at = corrected_now.replace(
        hour=hh,
        minute=mm,
        second=ss,
        microsecond=0,
    )

    rolled = False
    if target_at <= corrected_now:
        target_at += timedelta(days=1)
        rolled = True

    return target_at, rolled


def get_user_input():
    print("")
    print("=" * 52)
    print("  チケット先着購入 高精度タイマー v2")
    print("  Pydroid3安定版 / 事前URLアクセスなし")
    print("=" * 52)

    while True:
        print("")
        url = input("アクセスするURL:\n> ").strip()
        if url.startswith("http://") or url.startswith("https://"):
            break
        print("※ http:// または https:// から始めてください")

    print("")
    print("時刻を入力してください")
    print("例: 1000 -> 10:00:00 / 123456 -> 12:34:56")
    while True:
        try:
            hh, mm, ss = parse_time_input(input("> "))
            break
        except ValueError as exc:
            print("※ 入力エラー:", exc)

    print("")
    print("URL投入オフセットms（Enter=0推奨）")
    print("正の値: 遅らせる / 負の値: 指定時刻前に投入")
    while True:
        try:
            offset_ms = parse_dispatch_offset_ms(input("> "))
        except ValueError as exc:
            print("※ 入力エラー:", exc)
            continue

        if offset_ms < 0:
            print("※ 注意: 負の値は事前アクセスになる可能性があります。")
            ok = input("使う場合は yes と入力 > ").strip().lower()
            if ok not in ("yes", "y"):
                continue

        break

    return url, hh, mm, ss, offset_ms


# ========================================================
# カウントダウン
# ========================================================

def countdown_and_open(url, target_at, ntp, offset_ms):
    dispatch_at = target_at + timedelta(milliseconds=offset_ms)
    ntp_offset = ntp["offset"]

    print("")
    print("[設定] 販売予定時刻  :", target_at.strftime("%Y-%m-%d %H:%M:%S.%f"))
    print("[設定] URL投入予定   :", dispatch_at.strftime("%Y-%m-%d %H:%M:%S.%f"))
    print("[設定] 投入オフセット:", format_ms(offset_ms))
    print("[設定] NTP補正値     :", format_ms(ntp_offset * 1000))
    print("[設定] NTP RTT       :", format_ms(ntp["rtt"] * 1000))
    print("[設定] URL           :", url)
    print("[安全] URL投入予定時刻まで対象URLにはアクセスしません。")

    now = now_corrected(ntp_offset)
    if dispatch_at <= now:
        print("[エラー] URL投入予定時刻がすでに過去です。")
        return

    stop_event = threading.Event()
    wake_lock_acquired = acquire_termux_wake_lock()
    awake_thread = threading.Thread(
        target=keep_awake_loop,
        args=(stop_event,),
    )
    awake_thread.daemon = True
    awake_thread.start()

    remaining_for_warmup = (dispatch_at - now).total_seconds()
    if remaining_for_warmup >= WARMUP_MIN_REMAINING_SEC:
        warm_thread = threading.Thread(target=warm_up)
        warm_thread.daemon = True
        warm_thread.start()
    else:
        print("[ウォームアップ] 時刻が近いためスキップします。")

    print("")
    print("[待機中] カウントダウン開始")
    print("")

    fired_at = dispatch_at
    method = "not-run"

    try:
        while True:
            now = now_corrected(ntp_offset)
            remaining = (dispatch_at - now).total_seconds()

            if remaining <= 0:
                break

            if remaining > 10.0:
                print("  URL投入まで {:8.2f} 秒".format(remaining), end="\r")
                time.sleep(0.5)
            elif remaining > 1.0:
                print("  URL投入まで {:8.4f} 秒".format(remaining), end="\r")
                time.sleep(0.05)
            elif remaining > 0.1:
                time.sleep(0.005)
            elif remaining > BUSY_WAIT_WINDOW_SEC:
                time.sleep(0.001)
            else:
                break

        while now_corrected(ntp_offset) < dispatch_at:
            pass

        fired_at = now_corrected(ntp_offset)
        print("")
        print("")
        print("[起動] URL投入:", fired_at.strftime("%H:%M:%S.%f"))
        method = open_url(url)

    finally:
        stop_event.set()
        if wake_lock_acquired:
            release_termux_wake_lock()

    write_log(url, fired_at, target_at, dispatch_at, ntp, offset_ms, method)

    target_diff = (fired_at - target_at).total_seconds() * 1000
    dispatch_diff = (fired_at - dispatch_at).total_seconds() * 1000
    separator = "-" * 44

    print("")
    print(separator)
    print("販売予定時刻 :", target_at.strftime("%H:%M:%S.%f"))
    print("URL投入予定  :", dispatch_at.strftime("%H:%M:%S.%f"))
    print("実際の投入   :", fired_at.strftime("%H:%M:%S.%f"))
    print("販売との差   :", format_ms(target_diff))
    print("投入誤差     :", format_ms(dispatch_diff))
    print("ブラウザ方式 :", method)
    print("ログ保存先   :", LOG_FILE)
    print(separator)


# ========================================================
# メイン
# ========================================================

def main():
    print("[起動] ticket_opener_v2.py を開始します")

    try:
        ntp = sync_ntp()
        url, hh, mm, ss, offset_ms = get_user_input()

        corrected_now = now_corrected(ntp["offset"])
        target_at, rolled = build_target_datetime(hh, mm, ss, corrected_now)

        if rolled:
            print("[確認] 入力時刻は翌日として扱います。")

        dispatch_at = target_at + timedelta(milliseconds=offset_ms)
        remaining = (dispatch_at - corrected_now).total_seconds()

        if remaining <= 0:
            print("[エラー] URL投入予定時刻がすでに過去です。")
            return

        print("")
        print("[確認] 約 {:.1f} 秒後にURLを投入します".format(remaining))
        print("省電力OFF・画面ON・バッテリー最適化除外を推奨します")

        countdown_and_open(url, target_at, ntp, offset_ms)

    except KeyboardInterrupt:
        print("")
        print("[中断] プログラムを終了しました。")


if __name__ == "__main__":
    main()
