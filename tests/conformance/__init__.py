"""The backend conformance suite (#1014).

One set of scenarios, run against every registered backend's fake
(:mod:`tests.conformance.backends`), each driving only the role protocols in
:mod:`sbxloop.vcs.protocol` and asserting only on the shared types in
:mod:`sbxloop.vcs.model`. A scenario declares the capabilities it relies on
with ``@pytest.mark.needs(...)``: a backend that reports one ``UNSUPPORTED``
skips the scenario with the capability named, and one that reports
``UNKNOWN`` fails it — an absent feature is a design input, an unreadable
one is a halt. This is the forge analogue of ``tests/fixtures/ecosystems/``:
what makes "supported" a testable claim rather than a table in a document.
"""
