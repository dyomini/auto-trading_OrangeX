"""격자 계산/어댑터 조립 헬퍼 — `main.py`(최초 기동)와 `engine/cycle_manager.py`
(COOLDOWN 이후 다음 사이클 재계산)가 공유한다. 원래 `main.py`에만 있었으나
`CycleManager`도 동일한 로직(현재가 조회 -> compute_grid -> 실행가능성/최소주문
검증)이 필요해 순환 임포트 없이 재사용하려고 분리했다.
"""
from __future__ import annotations

import logging
from decimal import ROUND_CEILING, Decimal
from typing import Optional

from config.settings import Settings
from exchange.base import ContractSpec, ExchangeAdapter
from exchange.orangex.adapter import OrangeXAdapter
from exchange.orangex.client import OrangeXClient
from exchange.orangex.ws_client import OrangeXWsClient
from exchange.paper import PaperAdapter
from strategy.feasibility import find_max_feasible_step, find_min_order_shortfalls
from strategy.grid import STEPS_PER_TIER, GridStepResult, compute_grid
from strategy.weights import load_weights

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """봇을 시작하면(또는 다음 사이클을 시작하면) 안 되는 상황(격자 계산 불일치,
    재시작 상태 불일치 등)에서 발생."""


def _require_resolved_direction(settings: Settings) -> str:
    """격자/어댑터 조립에는 확정된 방향("long"/"short")이 필요하다.

    `"auto"`/`"both"`는 `main.py`가 실제 방향을 정해 `model_copy`로 넘겨야 하는 값이라,
    여기까지 그대로 흘러들어오면 격자가 반대로 깔리거나(compute_grid) 주문이 조용히
    자동취소되는(position_side 불일치) 사고가 된다. `compute_grid()`도 결국 ValueError를
    내지만 그건 한참 뒤라, 조립 입구에서 명확히 막는다."""
    if settings.direction not in ("long", "short"):
        raise StartupError(
            f"격자/어댑터 조립에는 확정된 방향이 필요함 — direction={settings.direction!r}. "
            'main.py가 실제 방향을 정해 model_copy로 넘겨야 한다.'
        )
    return settings.direction


def build_market_data_adapter(settings: Settings) -> OrangeXAdapter:
    """주문 실행과 무관하게 현재가/계약스펙 조회 전용 — `/public/*`만 호출하므로
    API 키가 비어 있어도(예: paper 모드에서 키를 안 넣은 경우) 동작한다
    (docs/api-notes.md §6 항목15, OrangeXClient.call(authed=False)는 토큰을 요구하지 않음).
    이 어댑터는 공개 엔드포인트만 쓰므로 auth_grant_type이 실제로 안 쓰이지만, 아래
    build_execution_adapter()와 동일하게 명시해 둔다(일관성 + 향후 실수 방지)."""
    client = OrangeXClient(
        client_id=settings.api_key.get_secret_value(),
        client_secret=settings.api_secret.get_secret_value(),
        auth_grant_type="client_credentials",
    )
    return OrangeXAdapter(client)


def build_execution_adapter(
    settings: Settings, contract_spec: ContractSpec, shared_client: Optional[OrangeXClient] = None
) -> ExchangeAdapter:
    """`shared_client`는 direction="both"(2026-08-04, 롱/숏 동시 운용) 지원용 — 롱/숏
    두 어댑터가 REST 호출을 각자 별도의 `OrangeXClient`로 하면 클라이언트별 레이트리밋
    스로틀이 독립적으로 도는 바람에 계정 전체 한도(10 req/s, docs/api-notes.md §6 항목6)를
    합쳐서 넘길 수 있다 — 그래서 REST 클라이언트는 반드시 공유해야 한다. WS 클라이언트는
    반대로 절대 공유하면 안 된다: `OrangeXWsClient.notifications()`는 단일 소비자용 큐라
    두 FillRouter가 같은 큐를 나눠 가지면 서로의 체결 절반씩을 놓치게 된다 — 그래서
    watch_fills()용 연결은 방향마다 독립적으로 새로 만든다(같은 채널을 중복 구독해도
    두 연결이 각자 전체 스트림을 온전히 받으므로 문제없음).

    **2026-08-06 실전 사고로 발견**: `OrangeXClient`의 `auth_grant_type` 기본값은
    `"client_signature"`인데, 이 서명 기반 인증은 이 프로젝트 전체에서 단 한 번도
    안정적으로 성공한 적이 없다(`docs/api-notes.md` §2/§6 항목9 — "원인 불명으로
    항상 실패", WS에서도 동일 재현). 지금까지 라이브로 검증됐던 모든 주문/조회는
    전부 `scripts/`의 개별 진단 스크립트가 `auth_grant_type="client_credentials"`를
    직접 명시해서 우회한 것이었지, 이 함수(실제 `main.py`/`launcher.py`가 쓰는 운영
    경로)는 명시한 적이 없었다 — 즉 라이브 자동매매/즉시 진입은 인증이 언제 성공하고
    실패할지 예측 불가능한 상태로 지금까지 운용됐을 수 있다. 반드시 명시한다."""
    direction = _require_resolved_direction(settings)
    if settings.trading_mode == "live":
        client = shared_client or OrangeXClient(
            client_id=settings.api_key.get_secret_value(),
            client_secret=settings.api_secret.get_secret_value(),
            auth_grant_type="client_credentials",
        )
        # watch_fills()에 필요 — REST용 OrangeXClient와 별도 연결(exchange/orangex/
        # ws_client.py). 연결/인증/구독 자체는 라이브 확인됨(docs/api-notes.md §6 항목19)
        # 이지만 실제 체결 메시지 스키마(특히 fee)는 아직 미검증 — OrangeXAdapter 참고.
        ws_client = OrangeXWsClient(
            client_id=settings.api_key.get_secret_value(),
            client_secret=settings.api_secret.get_secret_value(),
        )
        return OrangeXAdapter(client, position_side=direction, ws_client=ws_client)
    return PaperAdapter(
        instrument=settings.symbol,
        contract_spec=contract_spec,
        initial_equity=settings.equity_usdt,
        leverage=settings.leverage,
        maker_fee=settings.maker_fee,
        taker_fee=settings.taker_fee,
    )


async def build_grid_rows(
    settings: Settings,
    market_data_adapter: OrangeXAdapter,
    contract_spec: ContractSpec,
) -> list[GridStepResult]:
    """실시간 현재가를 base_price로 잡아 격자를 계산하고, SPEC이 요구하는 사용자 지정
    max_stage 절삭(110번) + 실행가능성 절삭(66번) + 최소 주문 미달 검증(71번)을
    적용한다. 사이클마다(최초 기동 시에도, `CycleManager`가 COOLDOWN 이후 재호출할
    때도) 매번 새로 호출해야 한다 — 그 사이 가격이 움직였으므로 base_price를 다시
    잡아야 하기 때문이다.

    2026-08-17: SPEC 90번의 "ATR 급등 시 격자 간격 확대"는 사용자 결정으로 제거했다
    ("진입 근거에서 atr은 배제해"). tick은 이제 항상 `settings.grid_tick` 그대로다."""
    direction = _require_resolved_direction(settings)
    ticker = await market_data_adapter.get_ticker(settings.symbol)
    weights = load_weights()

    # 2026-08-04, "3k" 참고 스프레드시트(제까깟-마틴게이-3k.xlsx) 검증 결과 반영: max_stage
    # 절삭은 compute_grid() *이전에* weights 리스트 자체를 잘라서 넘긴다. compute_grid()의
    # weight_sum이 넘겨받은 weights 전체 합이라, 예전처럼 100개를 다 넘기고 결과 행만 잘라내면
    # 실제로 쓰이지도 않는 뒤쪽 tier들의 비중까지 분모에 남아 equity의 상당 부분이 어느
    # 단계에도 배정되지 않고 남아돌았다(MAX_STAGE=3 기준 약 79% 미배정, xlsx로 교차검증).
    # weights 자체를 잘라 넘기면 그 안에서 비중이 재정규화돼, 활성 tier들에 equity 전액이
    # 실제로 배정된다.
    max_stage_step_count = min(settings.max_stage * STEPS_PER_TIER, len(weights))
    if max_stage_step_count < len(weights):
        logger.info(
            "사용자가 설정한 max_stage=%d 단계로 격자 절삭(%d단계 → %d단계, SPEC 최대 단계 제한 "
            "— max_feasible_step과 별개로 사용자가 정한 상한). 비중도 이 %d단계 기준으로 "
            "재정규화됨(3k 참고 설계와 동일).",
            settings.max_stage, len(weights), max_stage_step_count, max_stage_step_count,
        )
        weights = weights[:max_stage_step_count]

    rows = compute_grid(
        direction=direction,
        base_price=ticker.last_price,
        tick=settings.grid_tick,
        weights=weights,
        equity=settings.equity_usdt,
        leverage=settings.leverage,
        maint_margin_rate=settings.maint_margin_rate,
        sl_pct=settings.sl_pct,
    )

    feasibility = find_max_feasible_step(rows)
    if not feasibility.all_feasible:
        logger.warning(
            "가용잔고가 음수로 전환되는 단계 발견 — %d단계까지만 사용(SPEC 66번 규정, "
            "최초 미달 단계 index=%s)",
            feasibility.max_feasible_step_count, feasibility.first_infeasible_index,
        )
        rows = rows[: feasibility.max_feasible_step_count]

    shortfalls = find_min_order_shortfalls(
        rows, min_qty=contract_spec.min_qty, min_notional=contract_spec.min_notional
    )
    if shortfalls:
        raise StartupError(
            f"{len(shortfalls)}개 단계가 최소 주문 수량/명목가치 미달인데 병합 로직이 "
            "아직 구현되지 않았음(docs/phase1-report.md 결정: 다음 단계에 합산 — 정책만 "
            f"확정, 구현은 미완). 첫 미달 단계: index={shortfalls[0].index}, "
            f"step_qty={shortfalls[0].step_qty}, notional={shortfalls[0].notional}. "
            + _min_equity_hint(settings, shortfalls, contract_spec)
        )
    return rows


def _min_equity_hint(
    settings: Settings, shortfalls: list, contract_spec: ContractSpec
) -> str:
    """"얼마를 넣어야 시작되는지"를 알려준다.

    단계 수량은 equity에 정비례하므로(step_margin = equity * w/Σw, step_qty =
    step_margin * leverage / price), 가장 크게 모자란 단계의 부족 배율을 현재 equity에
    곱하면 필요한 최소 자금이 나온다. `step_margin`이 0.1 단위로 quantize되는 탓에
    정확한 하한은 아니라 **근사치**임을 문구에 명시한다."""
    ratio = Decimal("1")
    for s in shortfalls:
        if s.step_qty > 0 and contract_spec.min_qty > 0:
            ratio = max(ratio, contract_spec.min_qty / s.step_qty)
        if s.notional > 0 and contract_spec.min_notional > 0:
            ratio = max(ratio, contract_spec.min_notional / s.notional)
    needed = (settings.equity_usdt * ratio).quantize(Decimal("1"), rounding=ROUND_CEILING)
    preset = f"GRID_PRESET={settings.grid_preset} " if settings.grid_preset else ""
    doubled = " (DIRECTION=both는 자금을 반씩 나눠 쓰므로 이 값의 2배가 필요하다)"
    return (
        f"현재 설정({preset}EQUITY_USDT={settings.equity_usdt}, LEVERAGE={settings.leverage})"
        f"으로 시작하려면 EQUITY_USDT가 약 {needed} USDT 이상이어야 한다(근사치)."
        + (doubled if settings.direction == "both" else "")
    )
