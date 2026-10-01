import uuid
from datetime import datetime, timedelta, timezone

from core.db.models import AgentMemory, Decision, Position, Strategy, Trade, Wallet
from tests.conftest import FakeSession
from worker.agent.memory import record_lesson, track_record


def _setup():
    session = FakeSession()
    wallet = Wallet(id=uuid.uuid4(), name="t", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)
    strategy = Strategy(id=uuid.uuid4(), type="ema_crossover", status="draft")
    session.add(strategy)
    return session, wallet, strategy


def test_record_lesson_captures_outcome_exit_reason_and_entry_reasoning():
    session, wallet, strategy = _setup()
    decision = Decision(id=uuid.uuid4(), wallet_id=wallet.id, proposal={"reasoning": "trend looked strong"}, risk_verdict="approved")
    session.add(decision)
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="SOLINR", side="buy", qty=1.0, entry_price=100.0,
                        stop_loss=95.0, take_profit=110.0, strategy_id=strategy.id, decision_id=decision.id,
                        opened_at=datetime.now(timezone.utc) - timedelta(hours=5))
    exit_trade = Trade(wallet_id=wallet.id, pair="SOLINR", side="sell", qty=1.0, price=95.0, fee=0.0, pnl=-5.5)

    record_lesson(session, wallet, position, exit_trade, "stop_loss")

    (memory,) = session.query(AgentMemory).all()
    assert memory.content.startswith("LOSS -5.50 INR (-5.50%) on SOLINR via ema_crossover")
    assert "by stop_loss after 5.0h" in memory.content
    assert "Why I entered: trend looked strong" in memory.content


def test_track_record_tallies_by_strategy_and_coin_worst_first_with_newest_lessons():
    session, wallet, strategy = _setup()
    for pair, pnl in (("SOLINR", -5.0), ("SOLINR", -3.0), ("BTCINR", 4.0)):
        session.add(Trade(wallet_id=wallet.id, pair=pair, side="sell", qty=1, price=1, fee=0, pnl=pnl, strategy_id=strategy.id))
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="buy", qty=1, price=1, fee=0))  # entry fill, ignored
    now = datetime.now(timezone.utc)
    session.add(AgentMemory(wallet_id=wallet.id, content="old lesson", created_at=now - timedelta(hours=1)))
    session.add(AgentMemory(wallet_id=wallet.id, content="new lesson", created_at=now))

    text = track_record(session, wallet.id)

    assert "By strategy: ema_crossover 1W/2L net -4.00 INR" in text
    assert "By coin: SOLINR 0W/2L net -8.00 INR; BTCINR 1W/0L net +4.00 INR" in text
    assert text.index("new lesson") < text.index("old lesson")


def test_track_record_with_no_closed_trades():
    session, wallet, _ = _setup()
    assert "no closed trades yet" in track_record(session, wallet.id)
