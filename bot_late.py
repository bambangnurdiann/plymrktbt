"""
bot_late.py
===========
Late Bot Polymarket — Multi-Coin (BTC, ETH, SOL, DOGE)
Versi improved dengan circuit breaker dan filter lebih ketat.

Perubahan utama:
  - Circuit breaker: cooldown saat streak loss, hard stop di 5x
  - Bad hour filter: blok jam UTC 2, 4, 7 (WR < 45% dari analisa)
  - Beat distance minimum dinaikkan (BTC: 40→60)
  - Entry zone dipersempit: 230s–270s
  - F5 odds spread wajib (min 0.05)
  - Strength gate minimum 0.4
  - Telegram /resume setelah hard stop

FIXES:
  - BUG #1: Arbiter sekarang di-lock TERLEPAS dari hasil order (stop 532 FOK spam)
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("logs/late_bot_live.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

from utils.colors import green, red, yellow, cyan, bold, dim, clear_screen
from utils.telegram_controller import TelegramController, CommandHandler
from fetcher.multi_ws import MultiWS
from fetcher.chainlink_monitor import ChainlinkMonitor
from engine.coin_engine import CoinEngine, SignalResult
from engine.signal_arbiter import SignalArbiter
from engine.result_tracker import ResultTracker
from engine.loss_analyzer import LossAnalyzer, BetContext
from engine.circuit_breaker import CircuitBreaker
from executor.polymarket import PolymarketExecutor

# ── Config ────────────────────────────────────────────────────
DRY_RUN        = os.getenv("DRY_RUN", "false").lower() == "true"
MIN_ODDS       = float(os.getenv("MIN_ODDS", "0.45"))
ENTRY_MIN      = float(os.getenv("LATE_ENTRY_MIN", "230"))    # dipersempit
ENTRY_MAX      = float(os.getenv("LATE_ENTRY_MAX", "270"))    # dipersempit
AUTO_REDEEM    = os.getenv("AUTO_REDEEM_ENABLED", "true").lower() == "true"
CLAIM_INTERVAL = int(os.getenv("CLAIM_CHECK_INTERVAL", "90"))
ACTIVE_COINS   = [c.strip().upper() for c in os.getenv("ACTIVE_COINS", "BTC").split(",") if c.strip()]

# Chainlink config
CL_ENABLED  = os.getenv("CHAINLINK_ARB_ENABLED", "true").lower() == "true"
CL_MIN_EDGE = float(os.getenv("CHAINLINK_MIN_EDGE", "0.10"))
CL_MIN_REM  = float(os.getenv("CHAINLINK_MIN_REM", "60"))     # dinaikkan dari 15
CL_MAX_REM  = float(os.getenv("CHAINLINK_MAX_REM", "240"))    # diturunkan dari 270
CL_VOL      = float(os.getenv("CHAINLINK_VOLATILITY", "0.001"))

# Circuit breaker config
CB_MAX_STREAK    = int(os.getenv("CB_MAX_STREAK", "3"))        # cooldown mulai
CB_HARD_STOP     = int(os.getenv("CB_HARD_STOP_STREAK", "5")) # hard stop
CB_SESSION_LIMIT = int(os.getenv("CB_SESSION_MAX_LOSS", "8")) # max loss per session
CB_MAX_DRAWDOWN  = float(os.getenv("CB_MAX_DRAWDOWN", "0.30")) # 30%

# Bad hours (WR < 45% dari loss analyzer) — bisa di-override via .env
_bad_hours_raw = os.getenv("BAD_HOURS_UTC", "2,4,7")
BAD_HOURS = set(int(h.strip()) for h in _bad_hours_raw.split(",") if h.strip().isdigit())


# ── Session Block ─────────────────────────────────────────────
def is_session_blocked() -> tuple:
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%H:%M")

    def to_min(t: str) -> int:
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    nm = to_min(now_str)
    blocks = []

    raw = os.getenv("SESSION_BLOCKS", "")
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if len(part) > 5 and "-" in part[5:]:
                times = part.rsplit("-", 1)
                if len(times) == 2:
                    blocks.append((times[0].strip(), times[1].strip()))

    s_start = os.getenv("SESSION_BLOCK_START", "00:00")
    s_end   = os.getenv("SESSION_BLOCK_END",   "00:01")
    if s_start and s_end and s_start != "00:00":
        blocks.append((s_start, s_end))

    for start, end in blocks:
        try:
            sm = to_min(start)
            em = to_min(end)
            blocked = (sm <= nm <= em) if sm <= em else (nm >= sm or nm <= em)
            if blocked:
                return True, f"SESSION BLOCK: {start}–{end} UTC"
        except Exception:
            continue

    # Bad hour check (dari loss analyzer)
    current_hour = datetime.now(timezone.utc).hour
    bad_hours_live = set(int(h.strip()) for h in os.getenv("BAD_HOURS_UTC", "2,4,7").split(",") if h.strip().isdigit())
    if current_hour in bad_hours_live:
        return True, f"BAD HOUR: UTC {current_hour:02d}:00 (WR < 45%)"

    return False, ""


# ── State ─────────────────────────────────────────────────────
class BotState:
    def __init__(self, bet_amount: float, starting_balance: float = 0.0):
        self.bet_amount       = bet_amount
        self.auto_bet         = True
        self.last_claim_check = 0.0
        self.total_claimed    = 0
        self.uptime_start     = time.time()
        self.manual_bet: Optional[tuple] = None
        self.odds: Dict[str, tuple] = {}
        self.tg  = TelegramController()
        self._last_low_balance_warn = 0.0
        self.loss_analyzer = LossAnalyzer()
        self.stop_requested = False

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(
            max_streak=CB_MAX_STREAK,
            hard_stop_streak=CB_HARD_STOP,
            session_max_loss=CB_SESSION_LIMIT,
            max_drawdown_pct=CB_MAX_DRAWDOWN,
            starting_balance=starting_balance,
        )
        self.circuit_breaker.set_telegram_callback(self.tg.send)


# ── Dashboard ─────────────────────────────────────────────────
def render_dashboard(
    state:    BotState,
    engines:  Dict[str, CoinEngine],
    mws:      MultiWS,
    results:  ResultTracker,
    executor: PolymarketExecutor,
    signals:  Dict[str, Optional[SignalResult]],
    arbiter:  SignalArbiter,
) -> None:
    clear_screen()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime  = int(time.time() - state.uptime_start)
    up_str  = f"{uptime//3600}h {(uptime%3600)//60}m {uptime%60}s"
    mode_c  = red if not DRY_RUN else yellow
    ws_c    = green if mws.status == "OK" else red
    W = 68

    def sep(char="-"): print("  " + char * W)
    def row(txt=""): print(f"  | {txt}")

    # ── Helpers ───────────────────────────────────────────────

    def _candle_bar(elapsed: float, total: float = 300,
                    zone_min: float = ENTRY_MIN, zone_max: float = ENTRY_MAX,
                    bar_width: int = 38) -> str:
        pos      = int(elapsed / total * bar_width)
        z_lo     = int(zone_min / total * bar_width)
        z_hi     = int(zone_max / total * bar_width)
        bar      = []
        for i in range(bar_width):
            if i == pos:
                bar.append("●")
            elif z_lo <= i <= z_hi:
                bar.append("▪")
            else:
                bar.append("─")
        raw = "[" + "".join(bar) + "]"
        result = ""
        for i, ch in enumerate(raw[1:-1]):
            if ch == "●":
                result += green("●") if z_lo <= i <= z_hi else yellow("●")
            elif ch == "▪":
                result += cyan("▪")
            else:
                result += dim(ch)
        return dim("[") + result + dim("]")

    def _liq_bar(liq_s: float, liq_l: float, width: int = 12) -> str:
        total = liq_s + liq_l
        if total <= 0:
            return dim("─" * width + " N/A")
        ratio_s = liq_s / total
        s_filled = int(ratio_s * width)
        l_filled = width - s_filled
        bar = red("▓" * s_filled) + green("░" * l_filled)
        pct = int(ratio_s * 100)
        color = red if pct > 60 else green if pct < 40 else yellow
        return f"{bar} {color(f'S:{pct}%')}"

    def _cvd_arrow(cvd: float) -> str:
        if cvd > 5_000:   return green("↑")
        if cvd < -5_000:  return red("↓")
        return yellow("→")

    def _mini_history(n: int = 12) -> str:
        resolved = [r for r in results._records if r.result in ("WIN", "LOSS")]
        recent   = resolved[-n:]
        parts    = []
        for r in recent:
            parts.append(green("W") if r.result == "WIN" else red("L"))
        if not parts:
            return dim("(belum ada)")
        return " ".join(parts)

    def _ev_per_bet() -> str:
        if results.total_bets == 0:
            return "N/A"
        ev = results.running_pnl / results.total_bets
        c  = green if ev >= 0 else red
        return c(f"{ev:+.2f}")

    def _roi_str() -> str:
        start = getattr(executor, "_starting_balance", 0) or executor.balance
        if start <= 0:
            return "N/A"
        roi = results.running_pnl / start * 100
        c   = green if roi >= 0 else red
        return c(f"{roi:+.1f}%")

    # ── Header ────────────────────────────────────────────────
    cb       = state.circuit_breaker
    cb_ok, _ = cb.can_bet()
    cb_str   = green(f"CB:OK({cb.state.consecutive_losses}L)") if cb_ok \
               else red(f"CB:⛔{cb.state.consecutive_losses}L")

    blocked, blk_reason = is_session_blocked()
    hour_now = datetime.now(timezone.utc).hour
    bh_str   = red(f"BAD-HOUR({hour_now}UTC)") if blocked and "BAD" in blk_reason \
               else red("BLOCKED") if blocked else green("OK")

    print()
    print(f"  +{'─'*W}+")
    row(f"{bold('LATE BOT')} {mode_c('LIVE' if not DRY_RUN else 'DRY-RUN')}  "
        f"|  {now_str}  |  ⏱{up_str}")
    row(f"WS:{ws_c(mws.status)}  Auto:{'ON' if state.auto_bet else yellow('OFF')}  "
        f"{cb_str}  Session:{bh_str}  Coins:{bold(' '.join(ACTIVE_COINS))}")
    print(f"  +{'─'*W}+")

    if not cb_ok:
        row(red(bold(f"  ⛔  {cb.status_str}")))
        sep()

    # ── Per-coin block ────────────────────────────────────────
    for coin in ACTIVE_COINS:
        data = mws.coins.get(coin)
        eng  = engines.get(coin)
        sig  = signals.get(coin)
        if not data or not eng:
            continue

        price     = data.price or 0
        beat      = eng.candle.beat_price
        elapsed   = eng.candle.elapsed
        remaining = eng.candle.remaining
        cvd2      = data.cvd_2min
        odds_up, odds_down = state.odds.get(coin, (0.5, 0.5))
        spread    = abs(odds_up - odds_down)

        beat_dist = abs(price - beat) if price and beat else None
        beat_str  = f"${beat:,.2f}" if beat else dim("N/A")
        dist_str  = (green if beat_dist and beat_dist >= 60 else
                     yellow if beat_dist else dim)(
                        f"Δ${beat_dist:.0f}" if beat_dist is not None else "Δ N/A")

        cl_edge_str = ""
        if sig and sig.chainlink_signal:
            e = sig.chainlink_signal.edge
            ec = green if e >= CL_MIN_EDGE else red
            cl_edge_str = f"  CL:{ec(f'{e:.3f}')}"

        if sig and sig.should_bet:
            mode_tag  = cyan(" [CL]") if sig.mode == "CHAINLINK" else ""
            sig_label = green(bold(
                f"▶ BET {sig.direction} str={sig.strength:.2f} conf={sig.confidence:.2f}"
            )) + mode_tag
        elif sig:
            fd    = sig.filter_details or {}
            fails = [k.upper() for k, (s, _) in fd.items() if s == "FAIL"]
            passes = [k.upper() for k, (s, _) in fd.items() if s == "PASS"]
            fail_str = red(",".join(fails)) if fails else ""
            pass_str = dim(f"+{len(passes)}OK") if passes else ""
            sig_label = (dim("SKIP ") + dim("[") + fail_str +
                         (dim("|") if fails and passes else "") +
                         pass_str + dim("]"))
        else:
            sig_label = dim("—")

        spread_c = green if spread >= 0.05 else yellow if spread >= 0.03 else red
        cvd_c    = green if cvd2 > 5000 else red if cvd2 < -5000 else yellow
        in_zone  = ENTRY_MIN <= elapsed <= ENTRY_MAX
        zone_lbl = green("IN-ZONE") if in_zone else (
            dim("PRE") if elapsed < ENTRY_MIN else dim("POST"))

        liq_no_data = (data.liq_short_3s == 0 and data.liq_long_3s == 0)

        row(f"{bold(f'[{coin}]')} ${price:>11,.2f}  "
            f"CVD:{cvd_c(f'{cvd2/1000:+.0f}k')}{_cvd_arrow(cvd2)}  "
            f"UP={odds_up:.2f}/DN={odds_down:.2f}  "
            f"Spread:{spread_c(f'{spread:.3f}')}")

        liq_section = _liq_bar(data.liq_short_3s, data.liq_long_3s) if not liq_no_data \
                      else red("Liq:NO-DATA    ")
        row(f"  Liq:[{liq_section}]  "
            f"Beat:{yellow(beat_str)}  {dist_str}{cl_edge_str}")

        bar = _candle_bar(elapsed)
        row(f"  {bar} t={elapsed:.0f}s rem={cyan(f'{remaining:.0f}s')}  "
            f"{zone_lbl}  {sig_label}")
        sep()

    # ── Signal Arbiter ────────────────────────────────────────
    row(bold("SIGNAL ARBITER"))
    sep()

    if arbiter.window_bet_done:
        row(green("  ✓ Sudah bet di window ini — menunggu window berikutnya"))
    elif not cb_ok:
        row(red(f"  ⛔ Circuit Breaker: {cb.status_str}  (/resume untuk lanjut)"))
    elif blocked:
        row(red(f"  ⛔ {blk_reason}"))
    else:
        candidates = [s for s in signals.values() if s and s.should_bet]
        if candidates:
            best = max(candidates, key=lambda s: s.strength)
            mode_tag = cyan(" [CHAINLINK]") if best.mode == "CHAINLINK" else ""
            row(green(bold(f"  ▶ READY: {best.coin} {best.direction} "
                           f"str={best.strength:.2f} conf={best.confidence:.2f}")) + mode_tag)
        else:
            skip_reasons = []
            for coin, sig in signals.items():
                if sig and not sig.should_bet:
                    fd    = sig.filter_details or {}
                    fails = [k for k, (s, _) in fd.items() if s == "FAIL"]
                    if fails:
                        skip_reasons.append(f"{coin}:{','.join(f.upper() for f in fails[:3])}")
            if skip_reasons:
                row(f"  {dim('Sinyal ada tapi terfilter:')}  {yellow(' | '.join(skip_reasons))}")
            else:
                row(f"  {dim('Menunggu sinyal...')}")
    sep()

    # ── Results ───────────────────────────────────────────────
    pnl_c = green if results.running_pnl >= 0 else red
    cb_s  = cb.state
    row(bold("RESULTS") + f"  {dim('─'*20)}  "
        f"EV/bet:{_ev_per_bet()}  ROI:{_roi_str()}")
    sep()
    row(f"  Saldo: {bold(f'${executor.balance:.2f}')}  "
        f"Bet:${state.bet_amount:.2f}  "
        f"PnL:{pnl_c(bold(f'${results.running_pnl:+.2f}'))}")
    row(f"  Bets:{results.total_bets}  "
        f"W:{green(str(results.wins))}  "
        f"L:{red(str(results.losses))}  "
        f"WR:{bold(f'{results.win_rate:.1f}%')}  "
        f"Streak: L{cb_s.consecutive_losses}/W{cb_s.consecutive_wins}  "
        f"SessL:{cb_s.session_losses}")

    row(f"  History: {_mini_history(12)}")

    if results.current_bet:
        cb_r    = results.current_bet
        d_c     = green if cb_r.direction == "UP" else red
        pending = int(time.time() - time.mktime(
            datetime.strptime(cb_r.timestamp, "%Y-%m-%d %H:%M:%S").timetuple()
        ))
        row(f"  {bold('⏳ ACTIVE:')} {cb_r.window_id}  "
            f"{d_c(bold(cb_r.direction))}  "
            f"${cb_r.bet_amount:.2f} @ {cb_r.odds:.4f}  "
            f"{dim(f'({pending}s ago)')}")

    sep()

    master   = engines[ACTIVE_COINS[0]]
    next_win = int(master.candle.remaining)
    row(f"  {dim('[A] Auto  [Ctrl+C] Stop')}   "
        f"{dim('Next window:')} {cyan(f'{next_win}s')}")
    print(f"  +{'─'*W}+\n")


# ── Odds updater ──────────────────────────────────────────────
async def odds_loop(state: BotState, engines: Dict[str, CoinEngine], executor: PolymarketExecutor) -> None:
    while True:
        for coin in ACTIVE_COINS:
            try:
                market = executor.get_active_market(coin)
                if market:
                    up, down = executor.get_odds(market)
                    state.odds[coin] = (up, down)
                    engines[coin].update_odds(up, down)
            except Exception as e:
                logger.debug(f"[Odds] {coin}: {e}")
        await asyncio.sleep(3)


# ── Keyboard ──────────────────────────────────────────────────
def setup_keyboard(state: BotState) -> None:
    import threading
    if not sys.stdin.isatty():
        logger.info("[KB] Tidak ada TTY — keyboard listener dinonaktifkan")
        return

    def _listen_windows():
        import msvcrt
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getwch().upper()
                if key == 'A':
                    state.auto_bet = not state.auto_bet
                    logger.info(f"[KB] Auto: {'ON' if state.auto_bet else 'OFF'}")
            time.sleep(0.05)

    def _listen_linux():
        import tty, termios, select
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    key = sys.stdin.read(1).upper()
                    if key == 'A':
                        state.auto_bet = not state.auto_bet
                        logger.info(f"[KB] Auto: {'ON' if state.auto_bet else 'OFF'}")
                    elif key == '\x03':
                        raise KeyboardInterrupt
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass

    try:
        import msvcrt
        threading.Thread(target=_listen_windows, daemon=True).start()
    except ImportError:
        try:
            import tty, termios
            threading.Thread(target=_listen_linux, daemon=True).start()
        except Exception as e:
            logger.info(f"[KB] Keyboard listener tidak tersedia: {e}")


# ── Claim ─────────────────────────────────────────────────────
def maybe_claim(state: BotState, executor: PolymarketExecutor) -> None:
    if not AUTO_REDEEM:
        return
    now = time.time()
    if now - state.last_claim_check < CLAIM_INTERVAL:
        return
    state.last_claim_check = now

    positions = executor.get_redeemable_positions()
    if not positions:
        return

    logger.info(f"[Claim] Ditemukan {len(positions)} posisi redeemable")
    claimed_count  = 0
    claimed_amount = 0.0

    for pos in positions:
        cid = pos.get("conditionId", "")
        if not cid:
            continue

        size = float(pos.get("size", pos.get("currentValue", 0)) or 0)

        ok = executor.claim_position(cid)
        if ok:
            state.total_claimed += 1
            claimed_count  += 1
            claimed_amount += size
            logger.info(f"[Claim] ✓ conditionId={cid[:16]}... size≈${size:.2f}")
        else:
            logger.warning(f"[Claim] ✗ Gagal claim conditionId={cid[:16]}...")
        time.sleep(1)

    if claimed_count > 0:
        old_balance = executor.balance
        executor.get_balance()
        balance_gain = executor.balance - old_balance

        logger.info(
            f"[Claim] Selesai: {claimed_count} posisi | "
            f"Saldo: ${old_balance:.2f} → ${executor.balance:.2f} (+${balance_gain:.2f})"
        )

        state.tg.send(
            f"💰 <b>Auto-Claim Berhasil</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Posisi diklaim : {claimed_count}\n"
            f"Total diklaim  : {state.total_claimed}\n"
            f"Saldo sekarang : <b>${executor.balance:.2f}</b>\n"
            f"Tambahan saldo : <b>+${balance_gain:.2f}</b>"
        )


# ── Execute bet ───────────────────────────────────────────────
def execute_bet(
    coin: str, direction: str,
    state: BotState, engines: Dict[str, CoinEngine], mws: MultiWS,
    results: ResultTracker, executor: PolymarketExecutor, arbiter: SignalArbiter,
    signal: Optional[SignalResult] = None,
) -> None:
    eng = engines.get(coin)
    if not eng:
        return

    # Circuit breaker check sebelum eksekusi
    cb_ok, cb_reason = state.circuit_breaker.can_bet()
    if not cb_ok:
        logger.info(f"[Bet] Diblok circuit breaker: {cb_reason}")
        return

    odds_up, odds_down = state.odds.get(coin, (0.5, 0.5))
    odds      = odds_up if direction == "UP" else odds_down
    beat      = eng.candle.beat_price or 0
    data      = mws.coins.get(coin)
    price     = data.get_price() if data else 0
    remaining = eng.candle.remaining
    market    = executor.get_active_market(coin, force_refresh=True)

    if not market:
        logger.warning(f"[Bet] Tidak ada market aktif untuk {coin}")
        state.tg.notify_error(f"Tidak ada market aktif untuk {coin}")
        return

    token_id = market["token_id_up"] if direction == "UP" else market["token_id_down"]
    logger.info(f"[Bet] {coin} {direction} ${state.bet_amount:.2f} @ {odds:.4f}")

    ok = executor.place_order(token_id=token_id, amount=state.bet_amount,
                               side="BUY", price=odds, direction=direction)

    # ── BUG #1 FIX ────────────────────────────────────────────
    # Lock arbiter TERLEPAS dari hasil order.
    # Sebelumnya hanya di-lock jika ok=True, menyebabkan bot retry
    # 532x di satu window karena loop 0.3 detik tidak berhenti.
    eng.mark_bet_done()
    arbiter.mark_executed()
    # ──────────────────────────────────────────────────────────

    if ok:
        cl_edge = cl_fair_odds = cl_vol = 0.0
        sig_mode = ""
        if signal:
            sig_mode = signal.mode
            if signal.chainlink_signal:
                cl_sig       = signal.chainlink_signal
                cl_edge      = cl_sig.edge
                cl_fair_odds = cl_sig.fair_odds
                cl_vol       = cl_sig.vol_calibrated

        cvd_2min = liq_s3 = liq_l3 = liq_s30 = liq_l30 = 0.0
        if data:
            cvd_2min = data.cvd_2min
            liq_s3   = data.liq_short_3s
            liq_l3   = data.liq_long_3s
            liq_s30  = data.liq_short_30s
            liq_l30  = data.liq_long_30s

        results.record_bet(
            window_id=eng.candle.window_id,
            direction=direction,
            bet_amount=state.bet_amount,
            odds=odds,
            beat_price=beat,
            remaining_secs=remaining,
            odds_spread=abs(odds_up - odds_down),
            beat_distance=abs((price or beat) - beat),
            signal_mode=sig_mode,
            cl_edge=cl_edge,
            cl_fair_odds=cl_fair_odds,
            cl_vol=cl_vol,
            cvd_2min=cvd_2min,
            liq_short_3s=liq_s3,
            liq_long_3s=liq_l3,
            liq_short_30s=liq_s30,
            liq_long_30s=liq_l30,
            coin=coin,
            market_id=market.get("market_id", ""),
        )
        logger.info(f"[Bet] ✓ {coin} {direction}")
        state.tg.notify_bet(
            coin=coin, direction=direction,
            amount=state.bet_amount, odds=odds,
            beat=beat, price=price or 0,
            window_id=eng.candle.window_id,
        )
    else:
        logger.warning(f"[Bet] ✗ {coin} {direction} — order gagal, window dilewati")
        state.tg.notify_error(f"Order FAILED: {coin} {direction} @ {odds:.4f}")


# ── Main loop ─────────────────────────────────────────────────
async def main_loop(
    state: BotState, engines: Dict[str, CoinEngine],
    mws: MultiWS, results: ResultTracker,
    executor: PolymarketExecutor, arbiter: SignalArbiter,
    cl_monitor=None,
) -> None:
    signals: Dict[str, Optional[SignalResult]] = {}
    last_dash     = 0.0
    last_balance  = 0.0
    last_resolved = ""
    _prev_window  = ""

    logger.info(f"[LateBot] Main loop — coins: {ACTIVE_COINS}")
    logger.info(f"[LateBot] Entry zone: {ENTRY_MIN}-{ENTRY_MAX}s")
    logger.info(f"[LateBot] Bad hours (UTC): {sorted(BAD_HOURS)}")
    logger.info(f"[LateBot] Circuit breaker: max_streak={CB_MAX_STREAK} hard_stop={CB_HARD_STOP}")
    asyncio.create_task(odds_loop(state, engines, executor))

    cmd_handler = CommandHandler(state.tg)

    while True:
        now = time.time()

        # 1. Telegram commands
        cmd = state.tg.get_pending_command()
        if cmd:
            if cmd.cmd == "/resume":
                state.circuit_breaker.force_resume()
                state.auto_bet = True
                state.tg.send("▶️ <b>Bot di-resume</b>\nCircuit breaker di-reset. Auto-bet aktif.")
            else:
                cmd_handler.process(cmd, state, results, engines, mws)

        if state.stop_requested:
            break

        # 2. Master clock
        master = engines[ACTIVE_COINS[0]]
        master.candle.update()
        current_win = master.candle.window_id

        if current_win != _prev_window:
            logger.info(f"[LateBot] Window baru: {_prev_window} → {current_win} | Reset arbiter.")
            arbiter.reset_for_window(current_win)
            _prev_window = current_win
        elif master.candle.elapsed < 3.0 and arbiter.window_bet_done:
            logger.warning(
                f"[LateBot] Failsafe reset: elapsed={master.candle.elapsed:.1f}s "
                f"tapi arbiter masih locked di window={current_win}. Reset paksa."
            )
            arbiter.reset_for_window(current_win + "_reset")
            arbiter.reset_for_window(current_win)
        else:
            arbiter.reset_for_window(current_win)

        blocked, _ = is_session_blocked()

        # 3. Circuit breaker check
        cb_ok, _ = state.circuit_breaker.can_bet()

        # 4. Tick semua coin
        for coin in ACTIVE_COINS:
            eng  = engines[coin]
            data = mws.coins.get(coin)
            if not data:
                signals[coin] = None
                continue
            price = data.get_price()
            if price and eng.candle.elapsed < 5:
                eng.candle.update()
                eng.candle.set_beat_price(price)
            signals[coin] = None if (blocked or not cb_ok) else eng.tick(data)

        # 5. Balance check
        if now - last_balance > 30:
            executor.get_balance()
            last_balance = now
            state.circuit_breaker.check_drawdown(executor.balance)
            if executor.balance < state.bet_amount * 3 and executor.balance > 0:
                if now - state._last_low_balance_warn > 3600:
                    state.tg.notify_low_balance(executor.balance, state.bet_amount)
                    state._last_low_balance_warn = now

        # 6. Daily summary
        state.tg.maybe_send_daily_summary(executor.balance, results.running_pnl)

        # 7. Claim
        maybe_claim(state, executor)

        # 8. Resolve hasil bet
        if results.current_bet:
            cb       = results.current_bet
            bet_coin = getattr(cb, "coin", "BTC")
            cb_eng   = engines.get(bet_coin, master)

            elapsed_new = cb_eng.candle.elapsed
            is_new_window = (cb.window_id != current_win)

            if is_new_window and cb.window_id != last_resolved:
                should_try_resolve = (
                    (5 <= elapsed_new <= 180)
                    or elapsed_new > 180
                )

                if should_try_resolve:
                    cp = None
                    if cl_monitor:
                        cp = cl_monitor.get_price(bet_coin)
                        if cp:
                            logger.info(f"[Resolve] Pakai Chainlink price: ${cp:,.2f}")
                    if not cp:
                        close_data = mws.coins.get(bet_coin) or mws.coins.get("BTC")
                        cp = close_data.price if close_data else None
                        if cp:
                            logger.warning(f"[Resolve] Chainlink N/A → fallback Hyperliquid: ${cp:,.2f}")

                    if cp:
                        market_id = getattr(cb, "market_id", "")

                        polymarket_confirmed = ResultTracker.query_polymarket_result(market_id) if market_id else None
                        api_confirmed = polymarket_confirmed is not None

                        if not api_confirmed and elapsed_new < 90:
                            logger.debug(
                                f"[Resolve] Polymarket API belum confirmed, "
                                f"elapsed={elapsed_new:.0f}s — tunggu sebentar lagi."
                            )
                        else:
                            rec = results.resolve_bet(cb.window_id, cp, market_id=market_id)
                            if rec:
                                last_resolved = rec.window_id

                                state.circuit_breaker.record_result(rec.result, rec.pnl)

                                now_utc = datetime.now(timezone.utc)
                                ctx = BetContext(
                                    timestamp=rec.timestamp,
                                    window_id=rec.window_id,
                                    direction=rec.direction,
                                    result=rec.result,
                                    bet_amount=rec.bet_amount,
                                    odds=rec.odds,
                                    beat_price=rec.beat_price,
                                    close_price=rec.close_price or cp,
                                    pnl=rec.pnl,
                                    remaining_secs=rec.remaining_secs,
                                    odds_spread=rec.odds_spread,
                                    beat_distance=rec.beat_distance,
                                    signal_mode=rec.signal_mode,
                                    cl_edge=rec.cl_edge,
                                    cl_fair_odds=rec.cl_fair_odds,
                                    cl_vol=rec.cl_vol,
                                    cvd_2min=rec.cvd_2min,
                                    liq_short_3s=rec.liq_short_3s,
                                    liq_long_3s=rec.liq_long_3s,
                                    liq_short_30s=rec.liq_short_30s,
                                    liq_long_30s=rec.liq_long_30s,
                                    hour_utc=rec.hour_utc,
                                    market_id=getattr(rec, "market_id", ""),
                                    resolve_source=getattr(rec, "resolve_source", ""),
                                )
                                state.loss_analyzer.record(ctx)

                                if results.total_bets % 20 == 0 and results.total_bets > 0:
                                    state.loss_analyzer.print_report()

                                resolve_src = getattr(rec, "resolve_source", "")
                                unconfirmed_note = (
                                    "\n⚠️ <i>Hasil estimasi (Polymarket API belum confirmed)</i>"
                                    if resolve_src == "CHAINLINK_CALC" else ""
                                )

                                state.tg.notify_result(
                                    coin=bet_coin,
                                    direction=rec.direction,
                                    result=rec.result,
                                    bet_amount=rec.bet_amount,
                                    payout=rec.payout,
                                    pnl=rec.pnl,
                                    running_pnl=results.running_pnl,
                                    beat=rec.beat_price,
                                    close_price=rec.close_price or cp,
                                    win_rate=results.win_rate,
                                    odds=rec.odds,
                                    extra_note=unconfirmed_note,
                                )

        # 9. Betting logic
        if not arbiter.window_bet_done:
            if state.manual_bet:
                c, d = state.manual_bet
                state.manual_bet = None
                execute_bet(c, d, state, engines, mws, results, executor, arbiter)
            elif state.auto_bet and cb_ok:
                valid = [s for s in signals.values() if s and s.should_bet]
                best  = arbiter.select(valid)
                if best:
                    logger.info(f"[Arbiter] {best.coin} {best.direction} str={best.strength:.2f} conf={best.confidence:.2f}")
                    execute_bet(best.coin, best.direction, state, engines, mws, results, executor, arbiter, signal=best)

        # 10. Dashboard
        any_zone = any(ENTRY_MIN - 10 <= e.candle.elapsed <= ENTRY_MAX + 10 for e in engines.values())
        if now - last_dash >= (0.5 if any_zone else 2.0):
            render_dashboard(state, engines, mws, results, executor, signals, arbiter)
            last_dash = now

        await asyncio.sleep(0.3)


# ── Startup ───────────────────────────────────────────────────
def startup_prompt() -> float:
    import argparse
    parser = argparse.ArgumentParser(description="Late Bot Polymarket Multi-Coin")
    parser.add_argument("--bet",     type=float, default=None)
    parser.add_argument("--live",    action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print()
    print(bold("="*64))
    print(bold("  🎯 LATE BOT POLYMARKET — MULTI COIN (IMPROVED)"))
    print(bold("="*64))
    print()
    print(f"  Coins      : {bold(', '.join(ACTIVE_COINS))}")
    print(f"  Entry zone : t={ENTRY_MIN:.0f}–{ENTRY_MAX:.0f}s (dipersempit)")
    print(f"  Bad hours  : UTC {sorted(BAD_HOURS)} (diblok, WR < 45%)")
    print(f"  CB streak  : cooldown mulai >{CB_MAX_STREAK}, hard stop >{CB_HARD_STOP}")
    print(f"  Auto Claim : {'ON' if AUTO_REDEEM else 'OFF'}")
    print()

    dry = args.dry_run or DRY_RUN
    if dry:
        print(yellow("  ⚠️  DRY RUN aktif\n"))

    if args.bet is not None:
        bet = args.bet
        print(f"  Bet/trade  : {bold(f'${bet:.2f} USDC')} (dari --bet)\n")
    else:
        while True:
            try:
                bet = float(input("  Nominal bet per trade (USDC): $").strip())
                if bet > 0:
                    break
            except ValueError:
                pass
            print(red("  Masukkan angka > 0"))

    print(f"  Bet/trade : {bold(f'${bet:.2f} USDC')}")
    print(f"  Mode      : {red('DRY RUN') if dry else green('LIVE TRADING')}\n")

    if not dry:
        if args.live:
            print(green("  ✓ --live flag detected"))
        else:
            if input(f"  Ketik {bold('LIVE')} untuk konfirmasi: ").strip() != "LIVE":
                print(yellow("  Dibatalkan."))
                sys.exit(0)
    else:
        if not args.live and sys.stdin.isatty():
            input("  Tekan Enter untuk mulai...")

    print(green("\n  ✓ Bot dimulai!\n"))
    return bet


async def run():
    bet      = startup_prompt()
    executor = PolymarketExecutor(dry_run=DRY_RUN)
    executor.get_balance()
    starting_balance = executor.balance

    state    = BotState(bet, starting_balance=starting_balance)
    mws      = MultiWS(ACTIVE_COINS)
    arbiter  = SignalArbiter(min_strength=0.4)
    results  = ResultTracker(csv_path="logs/late_bot_results.csv")

    cl_monitor = None
    if CL_ENABLED:
        cl_monitor = ChainlinkMonitor(coins=ACTIVE_COINS, poll_interval=2.5)
        logger.info(f"[LateBot] Chainlink Arb ENABLED — min_edge={CL_MIN_EDGE}")

    engines = {
        coin: CoinEngine(
            coin,
            entry_min=ENTRY_MIN,
            entry_max=ENTRY_MAX,
            min_odds=MIN_ODDS,
            chainlink_monitor=cl_monitor,
            cl_min_edge=CL_MIN_EDGE,
            cl_min_remaining=CL_MIN_REM,
            cl_max_remaining=CL_MAX_REM,
            min_strength=0.4,
            bad_hours=BAD_HOURS,
        )
        for coin in ACTIVE_COINS
    }

    setup_keyboard(state)
    await mws.connect()
    if cl_monitor:
        await cl_monitor.start()
    await asyncio.sleep(3)

    logger.info(f"[LateBot] Saldo: ${executor.balance:.2f}")
    if not DRY_RUN and executor.balance < bet:
        print(red(f"\n  ⚠️  Saldo ${executor.balance:.2f} < bet ${bet:.2f}"))

    state.tg.notify_start("Late Bot Multi-Coin (Improved)", bet, ACTIVE_COINS, DRY_RUN)

    try:
        await main_loop(state, engines, mws, results, executor, arbiter, cl_monitor=cl_monitor)
    except KeyboardInterrupt:
        pass
    finally:
        await mws.disconnect()
        if cl_monitor:
            await cl_monitor.stop()
        state.tg.notify_stop(results.total_bets, results.wins, results.losses, results.running_pnl)
        print(yellow("\n\n  Bot dihentikan."))
        print(f"  Hasil: {results.summary()}\n")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print(yellow("\n  Bot dihentikan."))
