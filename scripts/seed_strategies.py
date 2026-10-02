"""One-time bootstrap: one draft Strategy row per registry entry, at
its defaults. Safe to re-run -- skips a (type, params) pair that
already exists. Run manually: python -m scripts.seed_strategies
"""

from __future__ import annotations

from core.db.models import Strategy
from core.db.session import get_session
from core.strategies.registry import STRATEGY_REGISTRY


def main() -> None:
    with get_session() as session:
        for strategy_type, (params_class, _) in STRATEGY_REGISTRY.items():
            default_params = params_class().model_dump()
            existing = session.query(Strategy).filter_by(type=strategy_type).first()  # research keeps retired versions too
            if existing is not None:
                print(f"strategy '{strategy_type}' already exists, skipping")
                continue
            session.add(Strategy(type=strategy_type, params=default_params, status="draft", stats={}))
            print(f"seeded draft strategy '{strategy_type}' {default_params}")


if __name__ == "__main__":
    main()
