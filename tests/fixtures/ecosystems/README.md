# Ecosystem fixtures

Small, offline project skeletons — one per ecosystem sbxloop must not
mistake for a Python project. `tests/unit/test_ecosystems.py` walks them
through the generalization surface (language detection, the installer
allowlist, the project gate and the config-override lint) and asserts explicit expectations per fixture, so a regression names the
decision that changed rather than a snapshot that drifted.

Every file here is a manifest or a minimal stand-in; nothing is meant to
build. Add a directory, then add its row to `EXPECTATIONS` in the test.
