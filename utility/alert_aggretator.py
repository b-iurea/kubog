import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

class AlertAggregator:
    def __init__(self, alert_manager, flush_interval=60):
        self.alert_manager = alert_manager
        self.flush_interval = flush_interval
        self.event_buffer = defaultdict(list)  # key: (ns, workload, key) -> list of events
        self.lock = threading.Lock()
        self.running = True

        self._start_flusher()

    def _start_flusher(self):
        thread = threading.Thread(target=self._flush_loop, daemon=True)
        thread.start()

    def _flush_loop(self):
        while self.running:
            time.sleep(self.flush_interval)
            self.flush()

    def stop(self):
        self.running = False

    def buffer_event(self, event, alert_cfg, rule_key):
        key = (event["namespace"], event["workload"], rule_key)
        with self.lock:
            self.event_buffer[key].append(event)

    def flush(self):
        with self.lock:
            to_flush = dict(self.event_buffer)
            self.event_buffer.clear()

        for (ns, wl, key), events in to_flush.items():
            if not events:
                continue

            sample = events[0]
            count = len(events)
            message = f"🚨 {count}x {key} on {wl} (Namespace: {ns})"
            suggestion = self.alert_manager.config.get("kube-alerts", {}).get(key, {}).get("suggestion", "")

            enriched_event = {
                "namespace": ns,
                "workload": wl,
                "pod": sample.get("pod", "-"),
                "container": sample.get("container", "-"),
                "type": key,
                "exit_code": sample.get("exit_code"),
                "reason": sample.get("reason"),
                "message": f"{count} similar events aggregated.",
            }

            rule_stub = {
                "message": message,
                "suggestion": suggestion
            }

            self.alert_manager.send_teams_alert(enriched_event, rule_stub)
            self.alert_manager.send_email_alert(enriched_event, rule_stub)
