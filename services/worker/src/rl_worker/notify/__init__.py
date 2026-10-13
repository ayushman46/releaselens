"""Alert notifications (M10): ``analysis.results`` → ``notification_log`` → Slack / e-mail.

* :mod:`.consumer`: Kafka consumer of ``analysis.results`` (contract
  ``rl_core.events.analysis``), queues rows idempotently.
* :mod:`.enqueue`: :func:`~.enqueue.enqueue_for_analysis` and the pre-deploy hook
  :func:`~.enqueue.notify_risk`.
* :mod:`.dispatcher`: claims queued rows and delivers them with retries and backoff.
* :mod:`.channels` / :mod:`.render`: transports and message text.
"""

from rl_worker.notify.enqueue import enqueue_for_analysis, notify_risk

__all__ = ["enqueue_for_analysis", "notify_risk"]
