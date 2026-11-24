"""Entry point: one supervised Python process (a12 Q37).

DB at /data/app.sqlite (volume), HTTP on :8000 (Fly maps 443 -> 8000), poller
thread inside the same process. `python -m server.main`.
"""

import os

from . import (alerts, backup, budget, claims, config, db, health, httpd,
               liveness_transport, poller)


def main():
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    store = db.Store(config.DB_PATH)
    heartbeat = health.Heartbeat()
    heartbeat.touch()

    poller_kwargs = {}
    if config.ALERTS_ENABLED:
        registry = claims.Registry()
        alerts.seed_system_claims(registry)
        poller_kwargs = {
            "alert_builder": alerts.AlertBuilder(budget.Budget()),
            "registry": registry,
            "claim_ids": [config.ALERT_CLAIM_ID],
            "employer_transport_factory": liveness_transport.build_employer_transport,
            "liveness_tier": liveness_transport.liveness_tier,
        }

    if config.POLLER_ENABLED:
        poller.PollerThread(store, heartbeat, **poller_kwargs).start()
    if config.BACKUP_ENABLED:
        backup.BackupThread(config.DB_PATH, config.BACKUP_DIR).start()

    server = httpd.make_server(
        ("0.0.0.0", config.PORT), store=store, heartbeat=heartbeat
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
