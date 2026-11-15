"""A from-the-methodology model of MSCI India Standard Index rebalancing.

The MSCI Global Investable Market Indexes methodology is published and entirely
rules-based, so index membership is in principle computable from market data. What
the research firms sell is not a secret formula but better inputs: point-in-time
share counts, a careful shareholder-by-shareholder free-float classification, and
clean traded-value history. This package implements the published rules and is
explicit about where its inputs are weaker than theirs.
"""

__version__ = "0.1.0"
