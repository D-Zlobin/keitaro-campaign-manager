"""Five API backend integration: Geo list and offer autocomplete upstreams.

The browser never calls the Five API directly; credentials stay in backend
settings and results are translated into the stable application response
models from ``app.api.schemas``.

Assumed request/response contract (auth shape, search parameter, and the
mapping to Keitaro offer IDs remain unverified pending Five API
documentation/credentials — see README):

- ``GET {base}/geos`` -> ``[{"code": "de", "name": "Germany"}, ...]``
- ``GET {base}/offers?search=<term>`` ->
  ``[{"keitaro_offer_id": 123, "name": "..."}, ...]``
- authentication via ``Authorization: Bearer <key>``

Adjust ``app/five/models.py`` when the real contract is confirmed; the rest
of the application only sees :class:`GeoOption` and :class:`OfferOption`.
"""
