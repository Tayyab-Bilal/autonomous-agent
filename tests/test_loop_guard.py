from autonomous_agent.loop_guard import Level, LoopGuard


def feed(guard, calls):
    for name, args, result in calls:
        guard.record(name, args, result)


def test_identical_call_soft_on_third_hard_on_fourth():
    g = LoopGuard()
    feed(g, [("read", {"a": 1}, "same")] * 2)  # calls 1 and 2 ran
    assert [g.check("read", {"a": 1}).level for _ in range(2)] == [Level.SOFT, Level.HARD]


def test_two_identical_calls_are_still_fine():
    g = LoopGuard()
    feed(g, [("read", {"a": 1}, "same")])
    assert g.check("read", {"a": 1}).level is Level.OK


def test_soft_verdict_carries_a_nudge():
    g = LoopGuard()
    feed(g, [("read", {}, "same")] * 2)
    assert "different approach" in g.check("read", {}).message


def test_same_call_with_changing_results_is_not_a_loop():
    g = LoopGuard()
    feed(g, [("read", {}, i) for i in range(2)])
    assert g.check("read", {}).level is Level.OK


def test_two_call_cycle_detected():
    g = LoopGuard()
    feed(g, [("a", {}, 1), ("b", {}, 2)] * 2)
    assert g.check("a", {}).level is Level.SOFT
    assert g.check("c", {}).level is Level.OK  # breaking the cycle is fine


def test_four_call_cycle_detected():
    g = LoopGuard()
    feed(g, [(n, {}, n) for n in "wxyz"] * 2)
    assert g.check("w", {}).level is Level.SOFT


def test_polling_same_read_tool_detected():
    g = LoopGuard(read_tools=frozenset({"status"}))
    feed(g, [("status", {"i": i}, "pending") for i in range(4)])  # args differ, result never does
    assert g.check("status", {"i": 99}).level is Level.SOFT


def test_polling_rule_ignores_non_read_tools():
    g = LoopGuard(read_tools=frozenset())
    feed(g, [("status", {"i": i}, "pending") for i in range(4)])
    assert g.check("status", {"i": 99}).level is Level.OK


def test_varied_calls_not_flagged():
    g = LoopGuard(read_tools=frozenset({"read"}))
    for i in range(30):
        assert g.check("read", {"i": i}).level is Level.OK
        g.record("read", {"i": i}, {"v": i})
    assert g.nudges == 0


def test_polling_counts_consecutive_calls_only():
    g = LoopGuard(read_tools=frozenset({"status"}))
    feed(g, [("status", {"i": i}, "pending") for i in range(3)])
    feed(g, [("other_read", {}, "x")])
    feed(g, [("status", {"i": 9}, "pending")])
    assert g.check("status", {"i": 10}).level is Level.OK


def test_nudges_reset_after_a_genuinely_different_call():
    g = LoopGuard()
    feed(g, [("read", {}, "same")] * 2)
    assert g.check("read", {}).level is Level.SOFT and g.nudges == 1
    feed(g, [("write", {"new": True}, "ok")])
    assert g.nudges == 0
