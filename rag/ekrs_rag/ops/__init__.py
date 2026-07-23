"""Operator tooling for the EKRS RAG service.

This package hosts offline / read-only utilities used by humans operating
the service (rotation scripts, drift checks, etc). Nothing in here is
runtime-critical; the deployable RAG image does not require this
package to function.
"""
