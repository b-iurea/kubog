import re
from datetime import datetime
from kubernetes.client.rest import ApiException
from kubernetes.client import CustomObjectsApi

class RootCauseAnalyzer:
    def __init__(self, core_v1, apps_v1):
        self.core_v1   = core_v1
        self.apps_v1   = apps_v1
        self.metrics_api = CustomObjectsApi()

    def suggest(self, event):
        # Decidi il ramo in base al type o exit_code
        typ       = event.get("type")
        exit_code = event.get("exit_code")
        reason    = event.get("reason")

        # NODI
        if typ in ("MemoryPressure", "DiskPressure", "NotReady", "NotSchedulable"):
            return self._suggest_node(event)

        # WORKLOAD
        if exit_code is not None or typ == "OOM_KILLED":
            return self._suggest_oom(event)
        if typ == "PROBE_FAILURE":
            return self._suggest_probe(event)
        if reason == "CrashLoopBackOff":
            return self._suggest_crashloop(event)
        if typ == "TERMINATION":
            return self._suggest_termination(event)

        return None

    # ── HELPERS ─────────────────────────────────────────────────────────────

    def _parse_mem(self, mem_str):
        units = {"Ki":1/1024, "Mi":1, "Gi":1024}
        m = re.match(r"^([0-9.]+)([KMG]i)?$", mem_str or "")
        if not m: return 0.0
        val, unit = float(m.group(1)), (m.group(2) or "Mi")
        return val * units.get(unit, 1)

    def _get_live_usage(self, ns, pod_name, container):
        try:
            pods = self.metrics_api.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", "pods", ns
            )["items"]
            for item in pods:
                if item["metadata"]["name"] == pod_name:
                    for c in item["containers"]:
                        if c["name"] == container:
                            return self._parse_mem(c["usage"]["memory"])
        except ApiException:
            pass
        return None

    # ── SUGGESTIONS ─────────────────────────────────────────────────────────

    def _suggest_oom(self, event):
        ns      = event["namespace"]; pod = event["pod"]; ctr = event["container"]
        wl_kind, wl_name = event["workload"].split("/",1)
        # recupera spec
        try:
            dep = self.apps_v1.read_namespaced_deployment(wl_name, ns)
        except ApiException:
            return None

        # trova container e limiti
        for c in dep.spec.template.spec.containers:
            if c.name != ctr: continue
            lim = c.resources.limits.get("memory") if c.resources and c.resources.limits else None
            if not lim: return "💡 No memory limit set—consider adding one."
            limit_mib = self._parse_mem(lim)
            usage = self._get_live_usage(ns, pod, ctr) or 0
            # consigliamo max(usage*1.2, limit*1.5)
            rec = max(usage * 1.2, limit_mib * 1.5, usage + 100)
            return (f"💡 Current usage: {usage:.1f}MiB of {limit_mib:.1f}MiB limit. "
                    f"Consider raising to ~{int(rec)}MiB.")
        return None

    def _suggest_probe(self, event):
        ns      = event["namespace"]; ctr = event["container"]
        wl_kind, wl_name = event["workload"].split("/",1)
        try:
            obj = self.apps_v1.read_namespaced_deployment(wl_name, ns)
        except ApiException:
            return None

        for c in obj.spec.template.spec.containers:
            if c.name != ctr: continue
            p = c.liveness_probe or c.startup_probe or c.readiness_probe
            if not p: return None
            delay = getattr(p, "initial_delay_seconds", None)
            period = getattr(p, "period_seconds", None)
            msg = "💡"
            if delay is not None:
                msg += f" initialDelay {delay}s→{delay*2}s."
            if period is not None:
                msg += f" period {period}s→{period*2}s."
            return msg
        return None

    def _suggest_crashloop(self, event):
        # spesso CrashLoop deriva da OOM o probe
        return "💡 Investigate container logs (`kubectl logs`) and check resource limits or probe settings."

    def _suggest_termination(self, event):
        code   = event.get("exit_code")
        reason = event.get("reason") or "Unknown"
        return f"💡 Container exited with code {code} reason '{reason}'. Check application logs."

    def _suggest_node(self, event):
        typ   = event["type"]
        node  = event.get("node")
        # raccogliamo metriche live
        metrics = self._get_node_metrics(node)
        cpu_u = metrics["cpu"]
        mem_u = metrics["memory"]

        map_sugg = {
            "MemoryPressure":   "Evict or throttle memory-hungry pods on this node.",
            "DiskPressure":     "Clean up disk or increase volume size on this node.",
            "NotReady":         "Node not ready: check network/health; `kubectl describe node`.",
            "NotSchedulable":   "Node cordoned: use `kubectl uncordon` if you want scheduling.",
        }
        base = map_sugg.get(typ, "")
        details = []
        if cpu_u is not None:
            details.append(f"CPU usage: {cpu_u:.2f} cores")
        if mem_u is not None:
            details.append(f"Mem usage: {mem_u:.1f}MiB")
        detail_str = (" • ".join(details)) if details else ""
        
        return f"💡 {base} {detail_str}".strip()
