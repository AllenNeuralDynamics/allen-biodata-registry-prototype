"""Registration Lambda local source package.

This package collects helpers that are extracted out of ``handler.py``
so they can be unit-tested and property-tested in isolation. The
Lambda runtime continues to load ``handler.handler`` directly; nothing
in this package is on the cold-path import chain unless ``handler``
imports from it explicitly.
"""
