"""Percolate detection core (§16 of docs/percolate-spec-v1.0.md).

Ingestion (sources.py) -> entity resolution (entity_resolution.py) ->
corroboration scoring (corroboration.py) -> threshold -> LLM interpretation
(interpret.py), orchestrated end to end by pipeline.py.
"""
