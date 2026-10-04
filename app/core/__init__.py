"""Core services shared by every part of the application.

Modules here are deliberately GUI-free and dependency-light so that they can be
used from the user interface, the command line, background workers and tests
without dragging Qt (or a model runtime) in.
"""
