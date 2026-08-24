"""Property-based tests for the Registration Lambda.

These tests carry the ``pytest.mark.property`` marker and are
expected to take significantly longer than the unit-test suite.
They run independently from ``tests/`` so a fast-feedback loop on
unit tests is not blocked by Hypothesis example generation.
"""
