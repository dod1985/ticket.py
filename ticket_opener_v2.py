"""
ticket_opener_v2.py
改良版：高精度チケット先着購入 ブラウザ起動ツール（Pydroid3向け）

改善点:
  1. NTPで端末時刻のズレを自動補正
  2. Chromeを直接起動（webbrowser非依存）
  3. ブラウザ事前タブ表示 → リロード方式
  4. 実行中のスリープ抑制（wake_lockもどき）
  5. サブプロセス方式のフォールバック対応

使用方法:
  python ticket_opener_v2.py
"""

import os
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime


# ========================================================
# 設定
# ========================================================

LOG_DIR = "/storage/emulated/0/000STRAGE/ticket.py"
LOG_FILE = os.path.join(LOG_DIR, "access_log.txt")

# NTPサーバーリスト（上から順に試す）
NTP_SERVERS = [
    "ntp.nict.jp",  # 日本標準時（最優先）
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
]

# Androidで試みるChromeのパッケージ名 / バイナリパス候補
CHROME_PACKAGES = [
    "com.android.chrome",
    "com.chrome.beta",
    "com.chrome.dev",
]

# PCでのChromeパス候補（Androidでは使わない、フォールバック用）
CHROME_PATHS_PC = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


# ========================================================
# NTP 時刻補正
# ========================================================

def get_ntp_offset(server: str, timeout: float = 2.0) -> float | None:
    """
    NTPサーバーに問い合わせてローカル時刻とのオフセット（秒）を返す。
    取得失敗時は None を返す。
    """
    ntp_epoch_delta = 2208988800  # 1900-01-01 → 1970-01-01 の秒数
    try:
        packet = b"\x1b" + b"\x00" * 47  # NTPリクエストパケット
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t_send = time.time()
            s.sendto(packet, (server, 123))
            data, _ = s.recvfrom(1024)
            t_recv = time.time()

        if len(data) < 48:
            return None

        # サーバー送信時刻（Transmit Timestamp: バイト40〜47）
        tx_sec = struct.unpack("!I", data[40:44])[0] - ntp_epoch_delta
        tx_frac = struct.unpack("!I", data[44:48])[0] / 2**32
        t_server = tx_sec + tx_frac

        # ラウンドトリップ中央点をローカル時刻として使う
        t_local = (t_send + t_recv) / 2
        return t_server - t_local

    except Exception:
        return None


def sync_ntp() -> float:
    """
    複数のNTPサーバーを試してオフセットを取得する。
    取得できなかった場合は 0.0（補正なし）を返す。
    """
    print("[NTP] 時刻同期を開始します...")
    for server in NTP_SERVERS:
        print(f"  → {server} に問い合わせ中...", end=" ", flush=True)
        offset = get_ntp_offset(server)
        if offset is not None:
            print(f"成功（オフセット: {offset * 1000:+.3f} ms）")
            return offset
        print("失敗")
    print("[NTP] すべてのサーバーへの接続に失敗。補正なしで続行します。")
    return 0.0


def now_corrected(offset: float) -> datetime:
    """NTPオフセット補正済みの現在時刻を返す"""
    return datetime.fromtimestamp(time.time() + offset)


# ========================================================
# ブラウザ起動
# ========================================================

def open_with_chrome_android(url: str) -> bool:
    """
    Android環境でamコマンドを使ってChromeを直接起動する。
    成功した場合 True を返す。
    """
    for pkg in CHROME_PACKAGES:
        try:
            cmd = [
                "am",
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
                "-n",
                f"{pkg}/com.google.android.apps.chrome.Main",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                print(f"[ブラウザ] Chrome({pkg})でURLを開きました")
                return True
        except Exception:
            continue
    return False


def open_with_intent_android(url: str) -> bool:
    """
    amコマンドでパッケージ指定なしにブラウザを起動する（フォールバック）。
    """
    try:
        cmd = ["am", "start", "-a", "android.intent.action.VIEW", "-d", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            print("[ブラウザ] Intentでブラウザを起動しました")
            return True
    except Exception:
        pass
    return False


def open_with_termux_open(url: str) -> bool:
    """
    termux-open-url コマンドで起動（Termux環境向けフォールバック）。
    """
    try:
        result = subprocess.run(
            ["termux-open-url", url],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            print("[ブラウザ] termux-open-urlで起動しました")
            return True
    except Exception:
        pass
    return False


def open_with_chrome_pc(url: str) -> bool:
    """
    PC環境でChrome/Chromiumを直接起動する（開発・動作確認向けフォールバック）。
    """
    for chrome_path in CHROME_PATHS_PC:
        if not os.path.exists(chrome_path):
            continue
        try:
            subprocess.Popen([chrome_path, url])
            print(f"[ブラウザ] {chrome_path}でURLを開きました")
            return True
        except Exception:
            continue
    return False


def open_url(url: str):
    """
    ブラウザでURLを開く。複数の方法を順番に試す。
    """
    # 1. Androidネイティブ（Chrome直接起動）
    if open_with_chrome_android(url):
        return
    # 2. Androidネイティブ（Intent汎用）
    if open_with_intent_android(url):
        return
    # 3. termux-open-url
    if open_with_termux_open(url):
        return
    # 4. PC向けChrome/Chromium
    if open_with_chrome_pc(url):
        return
    # 5. 最終フォールバック：Python標準のwebbrowser
    import webbrowser

    webbrowser.open(url)
    print("[ブラウザ] webbrowser.open()で起動しました（精度低下の可能性あり）")


# ========================================================
# ウォームアップ（事前タブ表示）
# ========================================================

def warm_up():
    """
    ブラウザをabout:blankで事前起動しておく。
    """
    print("[ウォームアップ] ブラウザを事前起動中...")
    try:
        open_url("about:blank")
        time.sleep(1.5)
        print("[ウォームアップ] 完了")
    except Exception as e:
        print(f"[ウォームアップ] 失敗: {e}")


# ========================================================
# 画面スリープ抑制（ビジーループ中の保険）
# ========================================================

def keep_awake_loop(stop_event: threading.Event):
    """
    画面スリープを防ぐためのダミースレッド。
    stop_event がセットされるまで動き続ける。
    """
    while not stop_event.is_set():
        # 何もしないが CPU を少し使うことでスリープを抑制
        time.sleep(0.2)


# ========================================================
# ログ
# ========================================================

def write_log(url: str, fired_at: datetime, scheduled_at: datetime, offset_ms: float):
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
    diff_ms = (fired_at - scheduled_at).total_seconds() * 1000
    line = (
        f"[{fired_at.strftime('%Y-%m-%d %H:%M:%S.%f')}] "
        f"URL={url} | "
        f"予定={scheduled_at.strftime('%H:%M:%S.%f')} | "
        f"端末誤差={diff_ms:+.3f}ms | "
        f"NTP補正={offset_ms:+.3f}ms\n"
    )
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"[ログ] {line.strip()}")


# ========================================================
# ユーザー入力
# ========================================================

def parse_time_input(raw: str):
    """
    1〜6桁の数字を時刻に変換する。
    1234   → 12:34:00.000
    123456 → 12:34:56.000
    12     → 12:00:00.000
    例外: 変換できない場合は ValueError を送出
    """
    raw = raw.strip().replace(":", "")  # コロンが混入しても吸収
    if not raw.isdigit():
        raise ValueError("数字以外が含まれています")
    # 6桁に右ゼロ埋め（足りない桁は0で補完）
    raw = raw.ljust(6, "0")[:6]
    hh = int(raw[0:2])
    mm = int(raw[2:4])
    ss = int(raw[4:6])
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("時刻の範囲外です")
    from datetime import time as dtime

    return dtime(hh, mm, ss, 0)


def get_user_input():
    print("\n" + "=" * 52)
    print("  チケット先着購入 高精度タイマー v2  (改良版)")
    print("=" * 52)

    # URL入力
    while True:
        url = input("\nアクセスするURL:\n> ").strip()
        if url.startswith("http://") or url.startswith("https://"):
            break
        print("  ※ http:// または https:// から始めてください")

    # 時刻入力
    print("\n時刻（例: 1000 → 10:00:00 / 123456 → 12:34:56）:")
    while True:
        raw = input("> ").strip()
        try:
            target_time = parse_time_input(raw)
            break
        except ValueError as e:
            print(f"  ※ 1000 や 1200 のような形式で入力してください（{e}）")

    now = datetime.now()
    target_dt = now.replace(
        hour=target_time.hour,
        minute=target_time.minute,
        second=target_time.second,
        microsecond=0,
    )
    return url, target_dt


# ========================================================
# メインカウントダウン
# ========================================================

def countdown_and_open(url: str, target_dt: datetime, ntp_offset: float):
    offset_ms = ntp_offset * 1000

    print(f"\n[設定] ターゲット時刻 : {target_dt.strftime('%Y-%m-%d %H:%M:%S.%f')}")
    print(f"[設定] NTP補正値      : {offset_ms:+.3f} ms")
    print(f"[設定] URL            : {url}")

    # スリープ抑制スレッド開始
    stop_event = threading.Event()
    awake_thread = threading.Thread(
        target=keep_awake_loop,
        args=(stop_event,),
        daemon=True,
    )
    awake_thread.start()

    # ウォームアップ（バックグラウンド）
    warm_thread = threading.Thread(target=warm_up, daemon=True)
    warm_thread.start()

    print("\n[待機中] カウントダウン開始... （画面をつけたまま待機してください）\n")

    # ─── 段階的スリープ ───
    while True:
        now = now_corrected(ntp_offset)
        remaining = (target_dt - now).total_seconds()

        if remaining <= 0:
            break

        if remaining > 10.0:
            print(f"  残り {remaining:8.2f} 秒", end="\r", flush=True)
            time.sleep(0.5)

        elif remaining > 1.0:
            print(f"  残り {remaining:8.4f} 秒", end="\r", flush=True)
            time.sleep(0.05)

        elif remaining > 0.1:
            time.sleep(0.005)

        elif remaining > 0.020:
            time.sleep(0.001)

        else:
            # 最後の20ms：ビジーループ
            break

    # ビジーループ（最高精度ゾーン）
    while now_corrected(ntp_offset) < target_dt:
        pass

    # ─── ブラウザ起動 ───
    fired_at = now_corrected(ntp_offset)
    print(f"\n\n[🚀 起動！] {fired_at.strftime('%H:%M:%S.%f')}")
    open_url(url)

    # スリープ抑制停止
    stop_event.set()

    # ログ＆精度レポート
    write_log(url, fired_at, target_dt, offset_ms)

    diff_ms = (fired_at - target_dt).total_seconds() * 1000
    print(f"\n{'─' * 40}")
    print(f"  予定時刻   : {target_dt.strftime('%H:%M:%S.%f')}")
    print(f"  実際の起動 : {fired_at.strftime('%H:%M:%S.%f')}")
    print(f"  誤差(補正後): {diff_ms:+.3f} ms")
    print(f"  NTP補正値  : {offset_ms:+.3f} ms")
    print(f"  ログ保存先 : {LOG_FILE}")
    print(f"{'─' * 40}")


# ========================================================
# エントリーポイント
# ========================================================

def main():
    try:
        # NTP時刻同期
        ntp_offset = sync_ntp()

        # ユーザー入力
        url, target_dt = get_user_input()

        # 未来時刻チェック（NTP補正込み）
        now = now_corrected(ntp_offset)
        if target_dt <= now:
            print("\n[エラー] 指定時刻はすでに過去です。")
            return

        remaining_total = (target_dt - now).total_seconds()
        print(f"\n[確認] 約 {remaining_total:.1f} 秒後（{target_dt.strftime('%H:%M:%S')}）に起動します")
        print("       ※ 実行中は省電力モードをオフ・画面をつけたままにしてください")

        # カウントダウン&起動
        countdown_and_open(url, target_dt, ntp_offset)

    except KeyboardInterrupt:
        print("\n\n[中断] プログラムを終了しました。")


if __name__ == "__main__":
    main()
