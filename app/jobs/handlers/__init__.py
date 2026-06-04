"""Per-kind job handlers. Each ``run_<kind>(app, job, publish)`` runs one
generation against the GPU backends, calling ``publish(event)`` for every
token/progress event and persisting the final artifact keyed by ``job.id``.
"""
