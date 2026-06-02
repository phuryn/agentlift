from skylift.cost import Usage, estimate_cost, price_for


def test_price_for_known_and_fallback():
    assert price_for("claude-haiku-4-5") == (1.00, 5.00)
    assert price_for("claude-sonnet-4-6") == (3.00, 15.00)
    # family fallback for an unknown exact id
    assert price_for("claude-opus-4-9-experimental") == (15.00, 75.00)


def test_estimate_cost_math():
    # 1,000,000 input @ $1 + 1,000,000 output @ $5 = $6.00
    c = estimate_cost(Usage(input_tokens=1_000_000, output_tokens=1_000_000), "claude-haiku-4-5")
    assert abs(c - 6.00) < 1e-9


def test_estimate_cost_with_cache():
    # cache read is 0.10x input price
    c = estimate_cost(Usage(cache_read=1_000_000), "claude-haiku-4-5")
    assert abs(c - 0.10) < 1e-9
