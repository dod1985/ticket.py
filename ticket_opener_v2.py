"""
ticket_opener_v2.py
改良版：高精度チケット先着購入 ブラウザ起動ツール（Pydroid3向け）

方針:
  - 対象URLへの事前アクセスは行わない
  - ウォームアップでは about:blank のみを開く
  - 指定時刻に初めて対象URLをブラウザへ渡す

改善点:
  1. NTPを複数回サンプリングし、RTTが最小の補正値を採用
  2. Pydroid3で相性が良い webbrowser.open を標準の起動方法に採用
  3. 対象URLへ事前アクセスせず、ブラウザのみ事前ウォームアップ
  4. Termux wake lock が使える場合は利用し、使えない場合も待機ループを維持
  5. 必要に応じてAndroid/Termux/PC起動へフォールバック
  6. 翌日跨ぎ、起動オフセット、失敗理由ログに対応

使用方法:
  python ticket_opener_v2.py
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import NamedTuple


# ========================================================
# 設定
# ========================================================

LOG_DIR = "/storage/emulated/0/000STRAGE/ticket_opener"
LOG_FILE = os.path.join(LOG_DIR, "access_log.txt")

# NTPサーバーリスト（上から順に試す）
NTP_SERVERS = [
    "ntp.nict.jp",  # 日本標準時（最優先）
    "time.cloudflare.com",
    "time.google.com",
    "pool.ntp.org",
]

NTP_SAMPLES_PER_SERVER = 5
NTP_TIMEOUT_SEC = 1.2
NTP_SAMPLE_INTERVAL_SEC = 0.12

# Androidで試みるChromeのパッケージ名
CHROME_PACKAGES = [
    "com.android.chrome",
    "com.chrome.beta",
    "com.chrome.dev",
]

# Androidのamコマンド候補
AM_COMMANDS = [
    "am",
    "/system/bin/am",
]

# PCでのChromeパス候補（Androidでは使わない、フォールバック用）
CHROME_PATHS_PC = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]

# ブラウザ起動順。
# pydroid_webbrowser_first: Pydroid3向け。webbrowser.openを最初に使う。
# android_direct_first: am startでChrome/Intentを先に試す。
BROWSER_OPEN_MODE = "pydroid_webbrowser_first"

# 最後だけビジーループする範囲。長くしすぎると端末負荷が上がります。
BUSY_WAIT_WINDOW_SEC = 0.015

# 本番URL投入まで十分な余裕がある場合だけウォームアップする。
# 近すぎる場合は about:blank 起動と本番URL投入の競合を避ける。
WARMUP_MIN_REMAINING_SEC = 3.0

# URL投入オフセット（ms）。
# 0.0: 指定時刻ちょうど。正の値: 指定時刻後に遅らせる。負の値: 指定時刻前に投入。
DEFAULT_DISPATCH_OFFSET_MS = 0.0
MAX_ABS_DISPATCH_OFFSET_MS = 5000.0


class NtpSample(NamedTuple):
    server: str
    offset: float
    rtt: float


class NtpSyncResult(NamedTuple):
    offset: float
    server: str
    rtt: float
    sample_count: int


class BrowserOpenResult(NamedTuple):
    method: str
    failures: list[tuple[str, str]]


# ========================================================
# NTP 時刻補正
# ========================================================

def get_ntp_sample(
    server: str,
    timeout: float = NTP_TIMEOUT_SEC,
) -> NtpSample | None:
    """
    NTPサーバーに問い合わせてローカル時刻とのオフセット（秒）とRTTを返す。
    取得失敗時は None を返す。
    """
    ntp_epoch_delta = 2208988800  # 1900-01-01 -> 1970-01-01 の秒数
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
        return NtpSample(
            server=server,
            offset=t_server - t_local,
            rtt=t_recv - t_send,
        )

    except Exception:
        return None


def sync_ntp() -> NtpSyncResult:
    """
    複数回のNTPサンプルから、RTTが最小のオフセットを採用する。
    取得できなかった場合は補正なしの結果を返す。
    """
    print("[NTP] 時刻同期を開始します...")
    for server in NTP_SERVERS:
        print(f"  -> {server} に問い合わせ中...")
        samples: list[NtpSample] = []

        for index in range(NTP_SAMPLES_PER_SERVER):
            sample = get_ntp_sample(server)
            if sample is None:
                print(f"     {index + 1}/{NTP_SAMPLES_PER_SERVER}: 失敗")
            else:
                samples.append(sample)
                print(
                    f"     {index + 1}/{NTP_SAMPLES_PER_SERVER}: "
                    f"offset={sample.offset * 1000:+.3f}ms, "
                    f"RTT={sample.rtt * 1000:.3f}ms"
                )
            time.sleep(NTP_SAMPLE_INTERVAL_SEC)

        if samples:
            best = min(samples, key=lambda item: item.rtt)
            print(
                "[NTP] 採用: "
                f"{best.server} / offset={best.offset * 1000:+.3f}ms / "
                f"RTT={best.rtt * 1000:.3f}ms / samples={len(samples)}"
            )
            return NtpSyncResult(
                offset=best.offset,
                server=best.server,
                rtt=best.rtt,
                sample_count=len(samples),
            )

    print("[NTP] すべてのサーバーへの接続に失敗。補正なしで続行します。")
    return NtpSyncResult(offset=0.0, server="none", rtt=0.0, sample_count=0)


def now_corrected(offset: float) -> datetime:
    """NTPオフセット補正済みの現在時刻を返す。"""
    return datetime.fromtimestamp(time.time() + offset)


# ========================================================
# ブラウザ起動
# ========================================================

def _command_output(result: subprocess.CompletedProcess) -> str:
    output = " ".join(
        part.strip()
        for part in (result.stdout or "", result.stderr or "")
        if part.strip()
    )
    if output:
        return f"returncode={result.returncode}, {output[:400]}"
    return f"returncode={result.returncode}"


def _run_command(
    cmd: list[str],
    method_name: str,
    failures: list[tuple[str, str]],
    timeout: float = 3.0,
) -> bool:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        failures.append((method_name, f"{type(exc).__name__}: {exc}"))
        return False

    if result.returncode == 0:
        return True

    failures.append((method_name, _command_output(result)))
    return False


def open_with_chrome_android(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    Android環境でamコマンドを使ってChromeを直接起動する。
    成功した場合、使用した方法名を返す。
    """
    for am_cmd in AM_COMMANDS:
        for pkg in CHROME_PACKAGES:
            # Activity名を固定せず、パッケージ指定だけでChromeに渡す。
            method = f"Chrome package ({pkg}) via {am_cmd}"
            cmd = [
                am_cmd,
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
                "-p",
                pkg,
            ]
            if _run_command(cmd, method, failures):
                print(f"[ブラウザ] {method} でURLを開きました")
                return method

            # 端末によっては明示Activity指定の方が通る場合があるためフォールバック。
            method = f"Chrome activity ({pkg}) via {am_cmd}"
            cmd = [
                am_cmd,
                "start",
                "-a",
                "android.intent.action.VIEW",
                "-d",
                url,
                "-n",
                f"{pkg}/com.google.android.apps.chrome.Main",
            ]
            if _run_command(cmd, method, failures):
                print(f"[ブラウザ] {method} でURLを開きました")
                return method

    return None


def open_with_intent_android(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    amコマンドでパッケージ指定なしにブラウザを起動する（フォールバック）。
    """
    for am_cmd in AM_COMMANDS:
        method = f"Android intent via {am_cmd}"
        cmd = [am_cmd, "start", "-a", "android.intent.action.VIEW", "-d", url]
        if _run_command(cmd, method, failures):
            print(f"[ブラウザ] {method} でブラウザを起動しました")
            return method
    return None


def open_with_termux_open(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    termux-open-url コマンドで起動（Termux環境向けフォールバック）。
    """
    method = "termux-open-url"
    if _run_command(["termux-open-url", url], method, failures):
        print("[ブラウザ] termux-open-url で起動しました")
        return method
    return None


def open_with_chrome_pc(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    PC環境でChrome/Chromiumを直接起動する（開発・動作確認向けフォールバック）。
    """
    for chrome_path in CHROME_PATHS_PC:
        if not os.path.exists(chrome_path):
            continue
        method = f"PC Chrome ({chrome_path})"
        try:
            subprocess.Popen([chrome_path, url])
        except Exception as exc:
            failures.append((method, f"{type(exc).__name__}: {exc}"))
            continue
        print(f"[ブラウザ] {chrome_path} でURLを開きました")
        return method
    failures.append(("PC Chrome", "Chrome/Chromium executable was not found"))
    return None


def open_with_webbrowser(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    Python標準のwebbrowserを使う。
    Pydroid3では、この方法がAndroid側のブラウザ起動に最も安定する場合がある。
    """
    import webbrowser

    method = "webbrowser.open"
    try:
        opened = webbrowser.open(url)
    except Exception as exc:
        failures.append((method, f"{type(exc).__name__}: {exc}"))
    else:
        if opened:
            print("[ブラウザ] webbrowser.open() でブラウザへURLを渡しました")
            return method
        failures.append((method, "webbrowser.open returned False"))

    return None


def open_url_android_direct_first(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    Androidのam startを優先する起動順。
    am startが使える環境では速い可能性があるが、Pydroid3では失敗する端末もある。
    """
    method = open_with_chrome_android(url, failures)
    if method is not None:
        return method

    method = open_with_intent_android(url, failures)
    if method is not None:
        return method

    method = open_with_termux_open(url, failures)
    if method is not None:
        return method

    method = open_with_chrome_pc(url, failures)
    if method is not None:
        return method

    return open_with_webbrowser(url, failures)


def open_url_pydroid_webbrowser_first(url: str, failures: list[tuple[str, str]]) -> str | None:
    """
    Pydroid3向けの起動順。
    失敗しやすいam startを待たず、まずwebbrowser.openで既定ブラウザへ渡す。
    """
    method = open_with_webbrowser(url, failures)
    if method is not None:
        return method

    method = open_with_termux_open(url, failures)
    if method is not None:
        return method

    method = open_with_chrome_android(url, failures)
    if method is not None:
        return method

    method = open_with_intent_android(url, failures)
    if method is not None:
        return method

    return open_with_chrome_pc(url, failures)


def open_url(url: str) -> BrowserOpenResult:
    """
    ブラウザでURLを開く。複数の方法を順番に試す。
    """
    failures: list[tuple[str, str]] = []

    if BROWSER_OPEN_MODE == "pydroid_webbrowser_first":
        method = open_url_pydroid_webbrowser_first(url, failures)
    elif BROWSER_OPEN_MODE == "android_direct_first":
        method = open_url_android_direct_first(url, failures)
    else:
        failures.append(("browser mode", f"unknown mode: {BROWSER_OPEN_MODE}"))
        method = open_url_pydroid_webbrowser_first(url, failures)

    if method is None:
        print("[ブラウザ] 起動コマンドをすべて試しましたが、成功を確認できませんでした")
        method = "failed"

    return BrowserOpenResult(method=method, failures=failures)


# ========================================================
# ウォームアップ（対象URLにはアクセスしない）
# ========================================================

def warm_up():
    """
    対象URLにアクセスせず、about:blankだけでブラウザを事前起動する。
    """
    print("[ウォームアップ] 対象URLにはアクセスせず、about:blankでブラウザを事前起動中...")
    try:
        open_url("about:blank")
        time.sleep(1.5)
        print("[ウォームアップ] 完了")
    except Exception as exc:
        print(f"[ウォームアップ] 失敗: {exc}")


# ========================================================
# スリープ抑制
# ========================================================

def acquire_termux_wake_lock() -> bool:
    """
    Termux環境でwake lockを取得する。Pydroid3では失敗しても続行する。
    """
    try:
        result = subprocess.run(
            ["termux-wake-lock"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        print("[wake lock] termux-wake-lock は利用できません（待機ループで続行）")
        return False

    if result.returncode == 0:
        print("[wake lock] termux-wake-lock を取得しました")
        return True

    print("[wake lock] termux-wake-lock の取得に失敗（待機ループで続行）")
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


def keep_awake_loop(stop_event: threading.Event):
    """
    画面スリープを完全には防げないが、待機中にプロセスを動かし続ける保険。
    端末側でも省電力OFF・画面常時ON・バッテリー最適化除外を推奨。
    """
    while not stop_event.is_set():
        time.sleep(0.2)


# ========================================================
# ログ
# ========================================================

def _format_failures(failures: list[tuple[str, str]]) -> str:
    if not failures:
        return "なし"
    return " / ".join(f"{method}: {detail}" for method, detail in failures[-5:])


def format_ms(value: float) -> str:
    return "{:+.3f} ms".format(value)


def write_log(
    url: str,
    fired_at: datetime,
    target_at: datetime,
    dispatch_at: datetime,
    ntp: NtpSyncResult,
    dispatch_offset_ms: float,
    browser_result: BrowserOpenResult,
):
    target_diff_ms = (fired_at - target_at).total_seconds() * 1000
    dispatch_diff_ms = (fired_at - dispatch_at).total_seconds() * 1000
    ntp_text = (
        f"NTP={ntp.server},"
        f"補正={ntp.offset * 1000:+.3f}ms,"
        f"RTT={ntp.rtt * 1000:.3f}ms,"
        f"samples={ntp.sample_count}"
    )
    line = (
        f"[{fired_at.strftime('%Y-%m-%d %H:%M:%S.%f')}] "
        f"URL={url} | "
        f"予定={target_at.strftime('%Y-%m-%d %H:%M:%S.%f')} | "
        f"URL投入予定={dispatch_at.strftime('%H:%M:%S.%f')} | "
        f"販売予定との差={target_diff_ms:+.3f}ms | "
        f"URL投入誤差={dispatch_diff_ms:+.3f}ms | "
        f"投入オフセット={dispatch_offset_ms:+.3f}ms | "
        f"{ntp_text} | "
        f"ブラウザ={browser_result.method} | "
        f"失敗履歴={_format_failures(browser_result.failures)}\n"
    )
    try:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(f"[ログ] 保存に失敗しました: {exc}")
        print(f"[ログ] 内容: {line.strip()}")
        return

    print(f"[ログ] {line.strip()}")


# ========================================================
# ユーザー入力
# ========================================================

def parse_time_input(raw: str):
    """
    1〜6桁の数字を時刻に変換する。
    1234   -> 12:34:00.000
    123456 -> 12:34:56.000
    12     -> 12:00:00.000
    例外: 変換できない場合は ValueError を送出
    """
    raw = raw.strip().replace(":", "")  # コロンが混入しても吸収
    if not raw.isdigit():
        raise ValueError("数字以外が含まれています")

    raw = raw.ljust(6, "0")[:6]
    hh = int(raw[0:2])
    mm = int(raw[2:4])
    ss = int(raw[4:6])
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError("時刻の範囲外です")

    from datetime import time as dtime

    return dtime(hh, mm, ss, 0)


def parse_dispatch_offset_ms(raw: str) -> float:
    """
    URL投入タイミングの微調整値をmsで返す。
    正の値は指定時刻後、負の値は指定時刻前。
    """
    raw = raw.strip()
    if not raw:
        return DEFAULT_DISPATCH_OFFSET_MS

    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("数値で入力してください") from exc

    if abs(value) > MAX_ABS_DISPATCH_OFFSET_MS:
        raise ValueError(f"絶対値は {MAX_ABS_DISPATCH_OFFSET_MS:.0f}ms 以下にしてください")

    return value


def build_target_datetime(target_time, corrected_now: datetime) -> tuple[datetime, bool]:
    """
    入力時刻を補正済み現在時刻の日付に当て、過去なら翌日に繰り越す。
    """
    target_dt = corrected_now.replace(
        hour=target_time.hour,
        minute=target_time.minute,
        second=target_time.second,
        microsecond=0,
    )
    rolled_to_tomorrow = False
    if target_dt <= corrected_now:
        target_dt += timedelta(days=1)
        rolled_to_tomorrow = True
    return target_dt, rolled_to_tomorrow


def get_user_input():
    print("\n" + "=" * 58)
    print("  チケット先着購入 高精度タイマー v2  (事前URLアクセスなし)")
    print("=" * 58)

    while True:
        url = input("\nアクセスするURL（指定時刻までこのURLにはアクセスしません）:\n> ").strip()
        if url.startswith("http://") or url.startswith("https://"):
            break
        print("  ※ http:// または https:// から始めてください")

    print("\n時刻（例: 1000 -> 10:00:00 / 123456 -> 12:34:56）:")
    while True:
        raw = input("> ").strip()
        try:
            target_time = parse_time_input(raw)
            break
        except ValueError as exc:
            print(f"  ※ 1000 や 1200 のような形式で入力してください（{exc}）")

    print("\nURL投入オフセットms（Enter=0推奨）")
    print("  正の値: 指定時刻後に遅らせる / 負の値: 指定時刻前に投入")
    while True:
        raw = input("> ").strip()
        try:
            dispatch_offset_ms = parse_dispatch_offset_ms(raw)
        except ValueError as exc:
            print(f"  ※ 入力エラー: {exc}")
            continue

        if dispatch_offset_ms < 0:
            print("  ※ 注意: 負の値は指定時刻より前に対象URLをブラウザへ渡す可能性があります。")
            confirm = input("     それでもこの値を使う場合は yes と入力してください > ").strip().lower()
            if confirm not in ("yes", "y"):
                print("  ※ オフセット入力に戻ります。0または正の値を推奨します。")
                continue
        break

    return url, target_time, dispatch_offset_ms


# ========================================================
# メインカウントダウン
# ========================================================

def countdown_and_open(
    url: str,
    target_at: datetime,
    ntp: NtpSyncResult,
    dispatch_offset_ms: float,
):
    dispatch_at = target_at + timedelta(milliseconds=dispatch_offset_ms)

    print(f"\n[設定] 販売予定時刻   : {target_at.strftime('%Y-%m-%d %H:%M:%S.%f')}")
    print(f"[設定] URL投入予定    : {dispatch_at.strftime('%Y-%m-%d %H:%M:%S.%f')}")
    print(f"[設定] 投入オフセット : {dispatch_offset_ms:+.3f} ms")
    print(f"[設定] NTP補正値      : {ntp.offset * 1000:+.3f} ms")
    print(f"[設定] NTP RTT        : {ntp.rtt * 1000:.3f} ms ({ntp.server})")
    print(f"[設定] URL            : {url}")
    print("[安全] 対象URLはURL投入予定時刻までブラウザへ渡しません。")

    now = now_corrected(ntp.offset)
    if dispatch_at <= now:
        print("\n[エラー] URL投入予定時刻がすでに過去です。オフセットを見直してください。")
        return

    stop_event = threading.Event()
    wake_lock_acquired = acquire_termux_wake_lock()
    awake_thread = threading.Thread(
        target=keep_awake_loop,
        args=(stop_event,),
        daemon=True,
    )
    awake_thread.start()

    remaining_for_warmup = (dispatch_at - now_corrected(ntp.offset)).total_seconds()
    if remaining_for_warmup >= WARMUP_MIN_REMAINING_SEC:
        warm_thread = threading.Thread(target=warm_up, daemon=True)
        warm_thread.start()
    else:
        print("[ウォームアップ] URL投入時刻が近いためスキップします。")

    print("\n[待機中] カウントダウン開始... （省電力OFF・画面ON推奨）\n")

    browser_result = BrowserOpenResult(method="not-run", failures=[])
    fired_at = dispatch_at
    try:
        while True:
            now = now_corrected(ntp.offset)
            remaining = (dispatch_at - now).total_seconds()

            if remaining <= 0:
                break

            if remaining > 10.0:
                print(f"  URL投入まで {remaining:8.2f} 秒", end="\r", flush=True)
                time.sleep(0.5)
            elif remaining > 1.0:
                print(f"  URL投入まで {remaining:8.4f} 秒", end="\r", flush=True)
                time.sleep(0.05)
            elif remaining > 0.1:
                time.sleep(0.005)
            elif remaining > BUSY_WAIT_WINDOW_SEC:
                time.sleep(0.001)
            else:
                break

        while now_corrected(ntp.offset) < dispatch_at:
            pass

        # ここで初めて対象URLをブラウザへ渡す。
        fired_at = now_corrected(ntp.offset)
        print(f"\n\n[起動] URL投入: {fired_at.strftime('%H:%M:%S.%f')}")
        browser_result = open_url(url)

    finally:
        stop_event.set()
        if wake_lock_acquired:
            release_termux_wake_lock()

    write_log(
        url=url,
        fired_at=fired_at,
        target_at=target_at,
        dispatch_at=dispatch_at,
        ntp=ntp,
        dispatch_offset_ms=dispatch_offset_ms,
        browser_result=browser_result,
    )

    target_diff_ms = (fired_at - target_at).total_seconds() * 1000
    dispatch_diff_ms = (fired_at - dispatch_at).total_seconds() * 1000
    target_diff_text = format_ms(target_diff_ms)
    dispatch_diff_text = format_ms(dispatch_diff_ms)
    ntp_offset_text = format_ms(ntp.offset * 1000)

    separator = "-" * 48
    print("\n" + separator)
    print("  販売予定時刻 :", target_at.strftime("%H:%M:%S.%f"))
    print("  URL投入予定  :", dispatch_at.strftime("%H:%M:%S.%f"))
    print("  実際の投入   :", fired_at.strftime("%H:%M:%S.%f"))
    print("  販売予定との差:", target_diff_text)
    print("  URL投入誤差  :", dispatch_diff_text)
    print("  NTP補正値    :", ntp_offset_text)
    print("  ブラウザ方式 :", browser_result.method)
    print("  ログ保存先   :", LOG_FILE)
    print(separator)


# ========================================================
# エントリーポイント
# ========================================================

def main():
    try:
        ntp = sync_ntp()
        url, target_time, dispatch_offset_ms = get_user_input()

        corrected_now = now_corrected(ntp.offset)
        target_at, rolled_to_tomorrow = build_target_datetime(target_time, corrected_now)
        if rolled_to_tomorrow:
            print("\n[確認] 入力時刻は今日すでに過ぎているため、翌日の時刻として扱います。")

        dispatch_at = target_at + timedelta(milliseconds=dispatch_offset_ms)
        remaining_total = (dispatch_at - corrected_now).total_seconds()
        if remaining_total <= 0:
            print("\n[エラー] URL投入予定時刻がすでに過去です。")
            return

        print(
            f"\n[確認] 約 {remaining_total:.1f} 秒後 "
            f"（URL投入予定 {dispatch_at.strftime('%Y-%m-%d %H:%M:%S')}）に起動します"
        )
        print("       ※ 実行中は省電力モードOFF・画面ON・バッテリー最適化除外を推奨します")

        countdown_and_open(url, target_at, ntp, dispatch_offset_ms)

    except KeyboardInterrupt:
        print("\n\n[中断] プログラムを終了しました。")


if __name__ == "__main__":
    main()
