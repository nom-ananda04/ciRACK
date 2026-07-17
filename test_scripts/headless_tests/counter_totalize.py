"""counter_totalize: reads the 34980A's built-in totalize counter (8301) at
each poll pass. Drives nothing external, so there's no teardown work."""

from btop_test_suite import Counter34980aControl

TEST_ID = "counter_totalize"
REQUIRED_DRIVER = "keysight_34980a"
KIND = "continuous"


def run(daq, inst, publish, state):
    if "counter" not in state:
        counter = Counter34980aControl()
        counter.configure(inst)
        state["counter"] = counter
        state["last_count"] = None
    counter = state["counter"]
    count = counter.read_count(inst)
    if count is not None:
        if count != state["last_count"]:
            counter.log.info(f"count = {count}")
            state["last_count"] = count
        publish({"counter_8301": count}, tags={"subsystem": "counter_34980a"})


def teardown(state, daq, inst):
    pass  # nothing external is driven by this test -- no teardown needed
