"""1년치 BTC 1분봉으로 3k 프리셋 `DIRECTION=auto` / `DIRECTION=both` 백테스트.

봇의 순수 계산 모듈(strategy/*, config/presets.py, exchange/base.round_qty_to_step)을
**그대로 import해서** 쓴다 — 격자/평단/청산가/TP/SL/비중/RSI는 재구현하지 않는다.
재구현한 것은 engine/ 의 이벤트 루프(상태 머신)뿐이며, 그 의미는 아래 주석에 근거를 적었다.

체결 경로: 1분봉을 [시가 -> 저가/고가 -> 반대극단 -> 종가] 4틱으로 전개한다
(종가>=시가면 저가 먼저, 아니면 고가 먼저 — 업계 표준 관례).
"""
from __future__ import annotations

import csv
import sys
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Optional

# 리포지터리 루트를 import 경로에 넣는다 (scripts/backtest/ -> ../../)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.presets import resolve_preset
from exchange.base import round_qty_to_step
from strategy.feasibility import find_max_feasible_step, find_min_order_shortfalls
from strategy.grid import STEPS_PER_TIER, compute_grid
from strategy.indicators import compute_rsi
from strategy.weights import load_weights

getcontext().prec = 34

DATA = Path(__file__).parent / "data"

# --- 거래소 계약 스펙 (docs/phase2-report.md §1, 라이브 확정값) ---
QTY_STEP = Decimal("0.001")
MIN_QTY = Decimal("0.001")
MIN_NOTIONAL = Decimal("10")
MAKER_FEE = Decimal("0.0002")
TAKER_FEE = Decimal("0.0006")
MAINT_MARGIN_RATE = Decimal("0.005")

# --- 봇 설정 (.env: GRID_PRESET=3k, DIRECTION=auto/both, SL_ENABLED=false) ---
GRID_TICK = Decimal("50")
SL_PCT = Decimal("0.03")
COOLDOWN_MINUTES = 30
COMBINED_TP_ROE = Decimal("0.10")
HYBRID_RESET_MIN_TIER = 3          # engine/grid_engine.py
HYBRID_RESET_FRACTION = Decimal("0.5")
MANDATORY_SL_MIN_TIER = 3          # .env


# ----------------------------------------------------------------------------- 데이터
def load_bars() -> list[tuple[int, Decimal, Decimal, Decimal, Decimal]]:
    out = []
    with (DATA / "fut_1m.csv").open() as f:
        for row in csv.reader(f):
            if row[0] == "open_time_ms":
                continue
            out.append((int(row[0]), Decimal(row[1]), Decimal(row[2]), Decimal(row[3]), Decimal(row[4])))
    out.sort(key=lambda r: r[0])
    return out


def load_rsi_series() -> tuple[list[int], list[Decimal]]:
    """15분봉 종가로 RSI(14)를 미리 계산.

    engine/direction_selector.py는 `fetch_candles(limit=period+2)` -> 진행 중 봉 제거 ->
    종가 15개로 `compute_rsi(period=14)`를 부른다. 그 호출은 gains가 정확히 14개라
    Wilder 평활 루프가 한 번도 안 돌아 **단순 14기간 평균 RSI**가 된다 — 여기서도
    같은 함수에 같은 길이(15개)를 넘겨 동일한 값을 재현한다.
    """
    times, closes = [], []
    with (DATA / "spot_15m.csv").open() as f:
        for row in csv.reader(f):
            if row[0] == "open_time_ms":
                continue
            times.append(int(row[0]))
            closes.append(Decimal(row[1 + 3]))
    close_ms, rsis = [], []
    for j in range(14, len(closes)):
        close_ms.append(times[j] + 900_000)      # 이 봉이 "마감된" 시각
        rsis.append(compute_rsi(closes[j - 14: j + 1], period=14))
    return close_ms, rsis


def load_funding() -> list[tuple[int, Decimal]]:
    out = []
    with (DATA / "funding.csv").open() as f:
        for row in csv.reader(f):
            if row[0] == "funding_time_ms":
                continue
            out.append((int(row[0]), Decimal(row[1])))
    out.sort()
    return out


# ----------------------------------------------------------------------------- 계좌
@dataclass
class Position:
    qty: Decimal = Decimal("0")
    avg: Decimal = Decimal("0")     # 실제 체결 VWAP (수량 내림 반영) — 손익 계산용


@dataclass
class Account:
    balance: Decimal
    leverage: Decimal
    positions: dict[str, Position] = field(default_factory=lambda: {"long": Position(), "short": Position()})
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    liquidated: bool = False

    def upnl(self, price: Decimal) -> Decimal:
        p = self.positions
        return (p["long"].qty * (price - p["long"].avg)
                - p["short"].qty * (price - p["short"].avg))

    def notional(self, price: Decimal) -> Decimal:
        return (self.positions["long"].qty + self.positions["short"].qty) * price

    def used_margin(self) -> Decimal:
        p = self.positions
        return (p["long"].qty * p["long"].avg + p["short"].qty * p["short"].avg) / self.leverage

    def equity(self, price: Decimal) -> Decimal:
        return self.balance + self.upnl(price)

    def free_margin(self, price: Decimal) -> Decimal:
        return self.equity(price) - self.used_margin()

    def is_liquidatable(self, price: Decimal) -> bool:
        if self.positions["long"].qty == 0 and self.positions["short"].qty == 0:
            return False
        return self.equity(price) <= MAINT_MARGIN_RATE * self.notional(price)

    def open(self, direction: str, price: Decimal, qty: Decimal, fee_rate: Decimal) -> None:
        fee = qty * price * fee_rate
        self.balance -= fee
        self.fees_paid += fee
        pos = self.positions[direction]
        new_qty = pos.qty + qty
        pos.avg = (pos.avg * pos.qty + price * qty) / new_qty
        pos.qty = new_qty

    def close(self, direction: str, price: Decimal, qty: Decimal, fee_rate: Decimal) -> Decimal:
        pos = self.positions[direction]
        qty = min(qty, pos.qty)
        if qty <= 0:
            return Decimal("0")
        sign = Decimal("1") if direction == "long" else Decimal("-1")
        realized = qty * (price - pos.avg) * sign
        fee = qty * price * fee_rate
        self.balance += realized - fee
        self.fees_paid += fee
        pos.qty -= qty
        if pos.qty == 0:
            pos.avg = Decimal("0")
        return realized

    def liquidate(self, price: Decimal) -> None:
        """청산: 포지션 전량이 시장가로 강제 정리되고 유지증거금 정도만 남는다."""
        for d in ("long", "short"):
            self.close(d, price, self.positions[d].qty, TAKER_FEE)
        if self.balance < 0:
            self.balance = Decimal("0")
        self.liquidated = True

    def apply_funding(self, price: Decimal, rate: Decimal) -> None:
        p = self.positions
        pay = (p["long"].qty - p["short"].qty) * price * rate
        self.balance -= pay
        self.funding_paid += pay


# ----------------------------------------------------------------------------- 격자 사이클
@dataclass
class GridSide:
    """한 방향 격자 1사이클 — engine/grid_engine.py의 상태를 그대로 옮긴 것."""
    direction: str
    entries: list[Decimal]
    qtys: list[Decimal]            # round_qty_to_step 적용된 실제 주문 수량
    eng_avgs: list[Decimal]        # grid_rows[i].avg_price (엔진이 TP/SL 계산에 쓰는 값)
    tps: list[Decimal]
    sls: list[Decimal]
    tiers: list[int]
    filled: int = 0
    open_qty: Decimal = Decimal("0")       # engine.open_qty (내림 수량 누적)
    tp_price: Optional[Decimal] = None
    sl_price: Optional[Decimal] = None
    sl_ref: Optional[Decimal] = None       # crossing-trigger 기준선 (PaperAdapter 재현)
    hybrid_done: bool = False
    closed: bool = False
    margin_rejects: int = 0


def build_side(direction: str, base_price: Decimal, equity: Decimal, leverage: Decimal,
               weights_full: list[Decimal], max_stage: int) -> Optional[GridSide]:
    """engine/grid_setup.build_grid_rows()와 동일한 절차: max_stage 절삭 -> compute_grid
    -> 실행가능성 절삭 -> 최소주문 검증."""
    weights = weights_full[: max_stage * STEPS_PER_TIER]
    rows = compute_grid(
        direction=direction, base_price=base_price, tick=GRID_TICK, weights=weights,
        equity=equity, leverage=leverage, maint_margin_rate=MAINT_MARGIN_RATE, sl_pct=SL_PCT,
    )
    feas = find_max_feasible_step(rows)
    if not feas.all_feasible:
        rows = rows[: feas.max_feasible_step_count]
    if not rows:
        return None
    if find_min_order_shortfalls(rows, MIN_QTY, MIN_NOTIONAL):
        return None            # main.py는 StartupError로 기동 자체를 거부한다
    return GridSide(
        direction=direction,
        entries=[r.entry_price for r in rows],
        qtys=[round_qty_to_step(r.step_qty, QTY_STEP) for r in rows],
        eng_avgs=[r.avg_price for r in rows],
        tps=[r.target_tp_price for r in rows],
        sls=[r.sl_price for r in rows],
        tiers=[r.major_tier for r in rows],
    )


def tick_path(o: Decimal, h: Decimal, l: Decimal, c: Decimal) -> tuple[Decimal, ...]:
    return (o, l, h, c) if c >= o else (o, h, l, c)


# ----------------------------------------------------------------------------- 공통 실행
@dataclass
class Result:
    mode: str
    final_balance: Decimal
    peak_balance: Decimal
    min_balance: Decimal
    cycles: int
    tp_closes: int
    sl_closes: int
    hybrid_resets: int
    liquidated_at: Optional[int]
    liquidation_price: Optional[Decimal]
    fees: Decimal
    funding: Decimal
    margin_rejects: int
    max_drawdown_pct: Decimal
    equity_curve: list[tuple[int, Decimal]]
    max_steps_filled: int
    note: str = ""
    trades: list = field(default_factory=list)   # (ms, direction, base, reason, pnl, steps, balance)
    first_start_ms: Optional[int] = None


class Simulator:
    def __init__(self, bars, rsi_ms, rsi_vals, funding, sizing_equity: Decimal,
                 leverage: Decimal, max_stage: int, sl_enabled: bool, compound: bool,
                 path_mode: str = "standard"):
        self.path_mode = path_mode
        self.bars = bars
        self.rsi_ms = rsi_ms
        self.rsi_vals = rsi_vals
        self.funding = funding
        self.sizing_equity = sizing_equity
        self.leverage = leverage
        self.max_stage = max_stage
        self.sl_enabled = sl_enabled
        self.compound = compound
        self.weights = load_weights()

    def path(self, o, h, l, c):
        """1분봉 내부 가격 경로. "standard"는 종가 방향으로 왕복하는 업계 관례,
        "lowfirst"/"highfirst"는 봉 내부 순서 가정이 결과를 얼마나 바꾸는지 보는 민감도용."""
        if self.path_mode == "lowfirst":
            return (o, l, h, c)
        if self.path_mode == "highfirst":
            return (o, h, l, c)
        return (o, l, h, c) if c >= o else (o, h, l, c)

    def path_tag(self) -> str:
        return "" if self.path_mode == "standard" else f",{self.path_mode}"

    def rsi_at(self, ms: int) -> Optional[Decimal]:
        """ms 시점에 마지막으로 '마감된' 15분봉 기준 RSI."""
        i = bisect_right(self.rsi_ms, ms) - 1
        return self.rsi_vals[i] if i >= 0 else None

    # --- 진입/청산 프리미티브 ---
    def try_fill_entries(self, acct: Account, side: GridSide, price: Decimal) -> int:
        """격자 진입 지정가 체결. PaperAdapter는 크로스한 미체결 주문을 그 틱에 전부
        지정가로 채우고, GridEngine이 곧바로 다음 주문을 건다 — 1분 안에 WS 체결통보로
        여러 번 보충되므로 여기서는 크로스한 단계를 연쇄로 전부 채운다."""
        n = 0
        while side.filled < len(side.entries):
            i = side.filled
            ep = side.entries[i]
            hit = price <= ep if side.direction == "long" else price >= ep
            if not hit:
                break
            qty = side.qtys[i]
            need = qty * ep / self.leverage
            if acct.free_margin(price) < need:
                side.margin_rejects += 1
                break
            acct.open(side.direction, ep, qty, MAKER_FEE)
            side.open_qty += qty
            side.filled = i + 1
            n += 1
        return n

    def register_tp_sl(self, side: GridSide, sl_enabled: bool, last_price: Decimal) -> None:
        i = side.filled - 1
        side.tp_price = side.tps[i]
        if sl_enabled and side.tiers[i] >= MANDATORY_SL_MIN_TIER:
            side.sl_price = side.sls[i]
            side.sl_ref = last_price      # crossing-trigger: 등록 시점 조건 충족은 무시
        else:
            side.sl_price = None


def run_auto(sim: Simulator, start_balance: Decimal, sl_enabled: bool) -> Result:
    acct = Account(balance=start_balance, leverage=sim.leverage)
    bars = sim.bars
    fund_i = 0
    equity_curve: list[tuple[int, Decimal]] = []
    trades: list = []
    side: Optional[GridSide] = None
    cycle_start_ms = 0
    cycle_start_balance = start_balance
    next_start_ms = bars[0][0]
    cycles = tp_closes = sl_closes = hybrid = 0
    liq_ms = liq_px = None
    first_start = None
    peak = trough = start_balance
    max_dd = Decimal("0")
    max_steps = 0
    rejects = 0

    for bi, (ms, o, h, l, c) in enumerate(bars):
        while fund_i < len(sim.funding) and sim.funding[fund_i][0] <= ms:
            acct.apply_funding(c, sim.funding[fund_i][1])
            fund_i += 1

        if side is None and ms >= next_start_ms and not acct.liquidated:
            rsi = sim.rsi_at(ms)
            if rsi is not None:
                direction = "short" if rsi >= 50 else "long"
                eq = acct.balance if sim.compound else sim.sizing_equity
                new_side = build_side(direction, o, eq, sim.leverage, sim.weights, sim.max_stage)
                if new_side is None:
                    # main.py는 StartupError로 기동을 거부한다 — 여기서는 가능해질 때까지 대기
                    next_start_ms = ms + 60 * 60_000
                else:
                    side = new_side
                    cycles += 1
                    cycle_start_ms = ms
                    cycle_start_balance = acct.balance
                    if first_start is None:
                        first_start = ms

        for price in sim.path(o, h, l, c):
            if acct.liquidated:
                break
            if acct.is_liquidatable(price):
                acct.liquidate(price)
                liq_ms, liq_px = ms, price
                if side is not None:
                    trades.append((cycle_start_ms, side.direction, side.entries[0], "LIQUIDATED",
                                   acct.balance - cycle_start_balance, side.filled, acct.balance))
                side = None
                break
            if side is None:
                continue

            if sim.try_fill_entries(acct, side, price):
                sim.register_tp_sl(side, sl_enabled, price)
                max_steps = max(max_steps, side.filled)
            if side.filled == 0:
                continue

            if side.tp_price is not None:
                hit = price >= side.tp_price if side.direction == "long" else price <= side.tp_price
                if hit:
                    acct.close(side.direction, side.tp_price, side.open_qty, MAKER_FEE)
                    tp_closes += 1
                    trades.append((cycle_start_ms, side.direction, side.entries[0], "TP",
                                   acct.balance - cycle_start_balance, side.filled, acct.balance))
                    side = None
                    next_start_ms = ms + COOLDOWN_MINUTES * 60_000
                    break

            if side.sl_price is not None and side.sl_ref is not None:
                slp, d = side.sl_price, side.direction
                met = lambda p: (p <= slp) if d == "long" else (p >= slp)
                if not met(side.sl_ref) and met(price):
                    acct.close(side.direction, slp, side.open_qty, TAKER_FEE)
                    sl_closes += 1
                    trades.append((cycle_start_ms, side.direction, side.entries[0], "SL",
                                   acct.balance - cycle_start_balance, side.filled, acct.balance))
                    side = None
                    next_start_ms = ms + COOLDOWN_MINUTES * 60_000
                    break
                side.sl_ref = price

            if not side.hybrid_done and side.tiers[side.filled - 1] >= HYBRID_RESET_MIN_TIER:
                avg = side.eng_avgs[side.filled - 1]
                reached = price >= avg if side.direction == "long" else price <= avg
                if reached:
                    q = round_qty_to_step(side.open_qty * HYBRID_RESET_FRACTION, QTY_STEP)
                    if q >= MIN_QTY:
                        acct.close(side.direction, price, q, TAKER_FEE)
                        side.open_qty -= q
                        side.hybrid_done = True
                        hybrid += 1
                        sim.register_tp_sl(side, sl_enabled, price)

        if side is not None:
            rejects = max(rejects, side.margin_rejects)
        eq = acct.equity(c)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        trough = min(trough, eq)
        if bi % 1440 == 0:
            equity_curve.append((ms, eq))
        if acct.liquidated:
            break

    final = acct.balance if acct.liquidated else acct.equity(bars[-1][4])
    equity_curve.append((bars[min(bi, len(bars) - 1)][0], final))
    return Result(
        mode=f"auto(SL={'on' if sl_enabled else 'off'}{',compound' if sim.compound else ''}{sim.path_tag()})",
        final_balance=final, peak_balance=peak, min_balance=trough, cycles=cycles,
        tp_closes=tp_closes, sl_closes=sl_closes, hybrid_resets=hybrid,
        liquidated_at=liq_ms, liquidation_price=liq_px, fees=acct.fees_paid,
        funding=acct.funding_paid, margin_rejects=rejects, max_drawdown_pct=max_dd * 100,
        equity_curve=equity_curve, max_steps_filled=max_steps, trades=trades,
        first_start_ms=first_start,
    )


def run_both(sim: Simulator, start_balance: Decimal) -> Result:
    """DIRECTION=both — 양방향 동시 진입, 개별 TP/SL 없음(manual_mode=True).
    CombinedPnlMonitor가 합산 ROE >= COMBINED_TP_ROE일 때만 양쪽 전량 시장가 청산 후 즉시 재진입.

    격자 구성이 불가능한 시점(최소 주문 수량 미달)에는 `main.py`가 StartupError로 아예
    기동을 거부한다 — 여기서는 '가능해질 때까지 1시간 간격으로 재시도'로 모델링하고,
    실제로 처음 시작된 시각을 `first_start_ms`로 보고한다."""
    acct = Account(balance=start_balance, leverage=sim.leverage)
    bars = sim.bars
    fund_i = 0
    equity_curve: list[tuple[int, Decimal]] = []
    trades: list = []
    sides: Optional[dict[str, GridSide]] = None
    cycle_start_ms = 0
    cycle_start_balance = start_balance
    retry_after = bars[0][0]
    cycles = closes = 0
    liq_ms = liq_px = None
    first_start = None
    peak = trough = start_balance
    max_dd = Decimal("0")
    max_steps = 0
    rejects = 0

    for bi, (ms, o, h, l, c) in enumerate(bars):
        while fund_i < len(sim.funding) and sim.funding[fund_i][0] <= ms:
            acct.apply_funding(c, sim.funding[fund_i][1])
            fund_i += 1

        if sides is None and ms >= retry_after and not acct.liquidated:
            eq = (acct.balance if sim.compound else sim.sizing_equity) / 2
            ls = build_side("long", o, eq, sim.leverage, sim.weights, sim.max_stage)
            ss = build_side("short", o, eq, sim.leverage, sim.weights, sim.max_stage)
            if ls is None or ss is None:
                retry_after = ms + 60 * 60_000
            else:
                sides = {"long": ls, "short": ss}
                cycles += 1
                cycle_start_ms = ms
                cycle_start_balance = acct.balance
                if first_start is None:
                    first_start = ms

        for price in sim.path(o, h, l, c):
            if acct.liquidated:
                break
            if acct.is_liquidatable(price):
                acct.liquidate(price)
                liq_ms, liq_px = ms, price
                if sides is not None:
                    trades.append((cycle_start_ms, "both", sides["long"].entries[0], "LIQUIDATED",
                                   acct.balance - cycle_start_balance,
                                   max(s.filled for s in sides.values()), acct.balance))
                sides = None
                break
            if sides is None:
                continue

            for s in sides.values():
                sim.try_fill_entries(acct, s, price)
                max_steps = max(max_steps, s.filled)

            pnl = margin = fees = Decimal("0")
            for d, s in sides.items():
                if s.open_qty <= 0 or s.filled == 0:
                    continue
                avg = s.eng_avgs[s.filled - 1]
                pnl += s.open_qty * (price - avg) if d == "long" else s.open_qty * (avg - price)
                margin += s.open_qty * avg / sim.leverage
                fees += s.open_qty * avg * MAKER_FEE + s.open_qty * price * TAKER_FEE
            if margin > 0 and (pnl - fees) / margin >= COMBINED_TP_ROE:
                for d, s in sides.items():
                    acct.close(d, price, s.open_qty, TAKER_FEE)
                closes += 1
                trades.append((cycle_start_ms, "both", sides["long"].entries[0], "COMBINED_TP",
                               acct.balance - cycle_start_balance,
                               max(s.filled for s in sides.values()), acct.balance))
                sides = None
                break

        if sides is not None:
            rejects = max(rejects, max(s.margin_rejects for s in sides.values()))
        eq = acct.equity(c)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        trough = min(trough, eq)
        if bi % 1440 == 0:
            equity_curve.append((ms, eq))
        if acct.liquidated:
            break

    final = acct.balance if acct.liquidated else acct.equity(bars[-1][4])
    if sides is not None:
        # 관측이 끝난 시점에 아직 청산되지 않은 사이클 — "+10% 합산 익절"에 끝내 도달하지
        # 못하고 계속 들고 있는 상태다. 실현 손익이 아니므로 반드시 구분해서 기록한다.
        trades.append((cycle_start_ms, "both", sides["long"].entries[0], "OPEN_AT_END",
                       final - cycle_start_balance,
                       max(s.filled for s in sides.values()), final))
    equity_curve.append((bars[min(bi, len(bars) - 1)][0], final))
    return Result(
        mode=f"both{'(compound)' if sim.compound else ''}{sim.path_tag()}",
        final_balance=final, peak_balance=peak, min_balance=trough, cycles=cycles,
        tp_closes=closes, sl_closes=0, hybrid_resets=0, liquidated_at=liq_ms,
        liquidation_price=liq_px, fees=acct.fees_paid, funding=acct.funding_paid,
        margin_rejects=rejects, max_drawdown_pct=max_dd * 100, equity_curve=equity_curve,
        max_steps_filled=max_steps, trades=trades, first_start_ms=first_start,
    )


# ----------------------------------------------------------------------------- 리포트
import datetime as dt


def ts(ms: Optional[int]) -> str:
    return "-" if not ms else f"{dt.datetime.fromtimestamp(ms / 1000, dt.UTC):%Y-%m-%d %H:%M}"


def fmt(r: Result, start: Decimal) -> str:
    lines = [f"=== {r.mode} ==="]
    lines.append(f"  최종 자산       : {r.final_balance:,.2f} USDT  (수익률 {(r.final_balance/start-1)*100:+.2f}%)")
    lines.append(f"  최고/최저 자산  : {r.peak_balance:,.2f} / {r.min_balance:,.2f}")
    lines.append(f"  최대 낙폭(MDD)  : {r.max_drawdown_pct:.2f}%")
    lines.append(f"  첫 사이클 시작  : {ts(r.first_start_ms)}")
    lines.append(f"  사이클 수       : {r.cycles}  (익절 {r.tp_closes} / SL {r.sl_closes} / hybrid {r.hybrid_resets})")
    lines.append(f"  최대 진입 단계  : {r.max_steps_filled}")
    lines.append(f"  누적 수수료     : {r.fees:,.2f} USDT / 누적 펀딩비 {r.funding:,.2f} USDT")
    lines.append(f"  증거금부족 거부 : {r.margin_rejects}회")
    if r.liquidated_at:
        lines.append(f"  ** 강제청산: {ts(r.liquidated_at)} UTC @ {r.liquidation_price:,.1f} USDT "
                     f"-> 잔고 {r.final_balance:,.2f} (이후 시뮬레이션 중단) **")
    else:
        lines.append("  강제청산 없음")
    if r.trades:
        lines.append("  마지막 8사이클:")
        for t in r.trades[-8:]:
            lines.append(f"    {ts(t[0])} {t[1]:<5} base={t[2]:>10,.1f} {t[3]:<11} "
                         f"손익 {t[4]:>+10,.2f}  단계 {t[5]:>2}  잔고 {t[6]:>10,.2f}")
    return "\n".join(lines)


def monthly(r: Result) -> str:
    """월말 자산 스냅샷."""
    by = {}
    for ms, eq in r.equity_curve:
        k = f"{dt.datetime.fromtimestamp(ms/1000, dt.UTC):%Y-%m}"
        by[k] = eq
    return "  " + "  ".join(f"{k}:{v:,.0f}" for k, v in sorted(by.items()))


def main() -> None:
    bars = load_bars()
    rsi_ms, rsi_vals = load_rsi_series()
    funding = load_funding()
    print(f"1분봉 {len(bars):,}개  {ts(bars[0][0])} ~ {ts(bars[-1][0])} UTC")
    print(f"BTC {bars[0][1]:,.1f} -> {bars[-1][4]:,.1f} ({(bars[-1][4]/bars[0][1]-1)*100:+.1f}%)"
          f"  |  펀딩 정산 {len(funding)}회")

    start = Decimal("2000")
    max_stage, leverage = resolve_preset("3k", GRID_TICK)
    print(f"3k 프리셋: max_stage={max_stage} ({max_stage*STEPS_PER_TIER}단계), leverage={leverage}배, "
          f"시드 {start} USDT\n")

    runs = [
        ("auto",     run_auto, {"sl_enabled": False}, "standard"),
        ("auto+SL",  run_auto, {"sl_enabled": True},  "standard"),
        ("both",     run_both, {},                    "standard"),
        ("auto",     run_auto, {"sl_enabled": False}, "lowfirst"),
        ("auto",     run_auto, {"sl_enabled": False}, "highfirst"),
        ("both",     run_both, {},                    "lowfirst"),
        ("both",     run_both, {},                    "highfirst"),
    ]
    results = []
    for _label, fn, kwargs, pm in runs:
        sim = Simulator(bars, rsi_ms, rsi_vals, funding, start, leverage, max_stage,
                        sl_enabled=kwargs.get("sl_enabled", False), compound=False, path_mode=pm)
        r = fn(sim, start, **kwargs)
        results.append(r)
        print(fmt(r, start))
        print(monthly(r))
        print(flush=True)

    with (Path(__file__).parent / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mode", "final", "peak", "min", "mdd_pct", "cycles", "tp", "sl", "hybrid",
                    "fees", "funding", "first_start_ms", "liq_ms", "liq_px", "max_steps"])
        for r in results:
            w.writerow([r.mode, r.final_balance, r.peak_balance, r.min_balance, r.max_drawdown_pct,
                        r.cycles, r.tp_closes, r.sl_closes, r.hybrid_resets, r.fees, r.funding,
                        r.first_start_ms or "", r.liquidated_at or "", r.liquidation_price or "",
                        r.max_steps_filled])
    with (Path(__file__).parent / "curves.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mode", "ms", "equity"])
        for r in results:
            for ms, eq in r.equity_curve:
                w.writerow([r.mode, ms, eq])
    with (Path(__file__).parent / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mode", "start_ms", "direction", "base", "reason", "pnl", "steps", "balance"])
        for r in results:
            for t in r.trades:
                w.writerow([r.mode, *t])
    print("저장 완료: results.csv / curves.csv / trades.csv")


if __name__ == "__main__":
    main()
