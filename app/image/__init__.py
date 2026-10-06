"""Image Studio (Stage F).

A local image studio: create, import, edit, generate, organise and reuse images,
and put them into a project scene.

The package is deliberately split so that the parts that never need a model -
importing, editing, organising, validating, saving - work with nothing installed
at all.  Generation sits behind a provider contract in
:mod:`app.image.provider`, and the application reports honestly when no backend
is present rather than inventing a picture.

Importing this package loads nothing heavy: no model, no GPU probe, no network.
"""

from __future__ import annotations

__all__: list[str] = []
