import time
from datetime import datetime

class APIProfiler:
    def __init__(self):
        # in-memory storage dei log
        self.records = []

    def profile(self, method, resource, namespace, func):
        start = time.time()
        status_code = 0
        try:
            result = func()
            status_code = getattr(result, 'status', 200)
            return result
        except Exception as e:
            status_code = getattr(e, 'status', 500)
            raise
        finally:
            end = time.time()
            duration_ms = int((end - start) * 1000)
            self.records.append({
                "timestamp": datetime.utcnow(),
                "method": method,
                "resource": resource,
                "namespace": namespace or "",
                "duration_ms": duration_ms,
                "status_code": status_code
            })
