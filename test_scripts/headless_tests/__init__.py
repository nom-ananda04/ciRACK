"""Each headless_rack_control.py test lives in its own file here, fully
self-contained (own imports, own constants, own setup/run/teardown) so
debugging one test never requires reading the others. The one exception is
mux_rig.py, which holds ONLY the Keysight SW_AO_MUX physical wiring table
shared by fgen_sweep.py and ain_ao_route.py -- both round-robin the same
physical switch, so that one wiring truth lives in one place instead of two
copies that could drift out of sync. Every other file has zero
cross-imports from its sibling test files.

Every test module exposes:
    TEST_ID: str                       -- matches headless_rack_control.config.json's "tests" list
    REQUIRED_DRIVER: str                -- which driver must be enabled for this test to run
    KIND: "one_shot" | "continuous"
    run(daq, inst, publish)             -- one_shot only, called once to completion
    run(daq, inst, publish, state)      -- continuous only, called every poll pass
    teardown(state, daq, inst)          -- continuous only, called once when the test's slot ends
"""
