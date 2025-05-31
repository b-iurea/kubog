#!/usr/bin/env python3
import os
import csv
import argparse
from datetime import datetime
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import time
import json
import threading
import pandas as pd
from collections import defaultdict
from utility.kube_alerts import KubeAlertManager
from kubernetes.client.rest import ApiException
from utility.api_profiler import APIProfiler
from utility.api_usage_analyzer import run_api_analysis
from utility.root_cause import RootCauseAnalyzer

# from utility.debugger_safety_patch import (
#     safe_write_csv,
#     safe_sort_dataframe,
#     safe_alert_config,
#     is_thread_running_for
# )



DEFAULT_NAMESPACE = "test"
INTERVAL_SEC = 60
OUTPUT_DIR = os.getenv('OUTPUT_DIR', '.')


class PodRestartDebugger:
    def __init__(self, args):
        self.args = args
        self.recorded_events = set()
        self.previous_states = {}
        self.node_status_cache = {}
        self.watchers = {}
        self.v1 = None
        self.apps_v1 = None
        self.monitored_workloads = self._parse_workloads()
        self.resource_versions = {}  # {namespace: resource_version}
        self.watch_retry_delay = 5  # seconds between retries
        self.max_retry_delay = 60  # maximum retry delay
        self.backoff_factor = 1.5  # exponential backoff factor
        self.pod_workloads = {}

        self._teams_warning_printed = False
        self._email_warning_printed = False

        self.root_cause = None

        self.all_recent_events = []
        self.metrics_available = None
        # Define all possible CSV columns upfront
        self.all_columns = [
            # Common fields
            "timestamp", "namespace", "type", "pod", "container", "workload", "resource_version",
            # Termination fields
            "exit_code", "reason", "finished_at", "message",
            # State change fields
            "from", "to",
            # Probe fields
            "probe_type", "probe_message",
            # Node fields
            "node", "conditions", "capacity", "allocatable"
        ]

        self.alert_manager = KubeAlertManager("kube-alerts.yaml")

        self.api_profiler = APIProfiler()  # Profilatore API

        self._warmup = True

    def _parse_workloads(self):
        """Parse workload filters from command line arguments"""
        workloads = {}
        if not self.args.workloads:
            return workloads
            
        for item in self.args.workloads:
            try:
                if '/' in item:
                    ns, wl = item.split('/')
                    workloads.setdefault(ns, []).append(wl)
                else:
                    for ns in self.args.namespaces:
                        workloads.setdefault(ns, []).append(item)
            except ValueError:
                print(f"Invalid workload format: {item}")
        return workloads

    def setup_clients(self):
        """Initialize all Kubernetes API clients"""
        try:
            if self.args.service_account:
                config.load_incluster_config()
                print("✅ Using in-cluster configuration")
            else:
                config.load_kube_config(context=self.args.context)
                print(f"✅ Using context: {self.args.context or 'current'}")

            self.v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()

            self.root_cause = RootCauseAnalyzer(self.v1, self.apps_v1)

            print("🧠 Root Cause Analyzer enabled!")


            return True
        except Exception as e:
            print(f"❌ Client setup failed: {e}")
            return False
        
    def _start_namespace_watcher(self):
        """
        🔄 Watcher per i namespace:
        In modalità --chaos, osserva l'aggiunta o rimozione di nuovi namespace.
        Quando un namespace viene aggiunto, lo inizia a monitorare automaticamente.
        Quando un namespace viene rimosso, lo esclude dal monitoraggio.
        """
        if not self.v1:
            return
        w = watch.Watch()

        def namespace_watch_loop():
            for event in w.stream(self.v1.list_namespace, timeout_seconds=0):
                ns = event["object"].metadata.name
                if event["type"] == "ADDED" and ns not in self.args.namespaces:
                    print(f"🆕 New namespace detected: {ns}")
                    self.args.namespaces.append(ns)
                    self._process_namespace(ns)
                    if self.args.watch:
                        self._start_watcher(ns)
                elif event["type"] == "DELETED" and ns in self.args.namespaces:
                    print(f"🗑️ Namespace removed: {ns}")
                    self.args.namespaces.remove(ns)

        threading.Thread(target=namespace_watch_loop, daemon=True).start()

    def _should_monitor(self, pod, namespace):
        """Check if pod should be monitored based on workload filters"""
        if not self.monitored_workloads:
            return True
            
        if not pod.metadata.owner_references:
            return False
            
        for owner in pod.metadata.owner_references:
            if owner.kind == 'ReplicaSet':
                try:
                    rs = self.apps_v1.read_namespaced_replica_set(
                        owner.name, 
                        namespace
                    )
                    if rs.metadata.owner_references:
                        for rs_owner in rs.metadata.owner_references:
                            if rs_owner.kind == 'Deployment':
                                if namespace in self.monitored_workloads:
                                    return rs_owner.name in self.monitored_workloads[namespace]
                except Exception:
                    continue
            elif owner.kind in ['Deployment', 'StatefulSet', 'Job', 'CronJob', 'DaemonSet']:
                if namespace in self.monitored_workloads:
                    return owner.name in self.monitored_workloads[namespace]
            
        return False

    def _get_workload(self, pod):
        """Identify the parent workload of a pod"""
        if not pod.metadata.owner_references:
            return "None"
            
        for owner in pod.metadata.owner_references:
            if owner.kind == 'ReplicaSet':
                try:
                    rs = self.api_profiler.profile(
                        "read", "replicasets", pod.metadata.namespace,
                        lambda: self.apps_v1.read_namespaced_replica_set(owner.name, pod.metadata.namespace)
                    )
                    if rs.metadata.owner_references:
                        for rs_owner in rs.metadata.owner_references:
                            if rs_owner.kind == 'Deployment':
                                return f"Deployment/{rs_owner.name}"
                except Exception:
                    continue
            elif owner.kind in ['Deployment', 'StatefulSet', 'Job', 'CronJob', 'DaemonSet']:
                return f"{owner.kind}/{owner.name}"
        return "Unknown"

    def _has_termination(self, container):
        """Check if container has terminated since last check"""
        if not (hasattr(container, 'last_state') and 
                container.last_state and 
                container.last_state.terminated):
            return False
            
        term = container.last_state.terminated
        uid = f"{term.finished_at}-{container.name}-terminated"
        if uid not in self.recorded_events:
            self.recorded_events.add(uid)
            return True
        return False

    def _get_message(self, pod, container_name):
        """Get termination message for a container"""
        try:
            for container in pod.status.container_statuses:
                if container.name == container_name:
                    if container.state.terminated and container.state.terminated.message:
                        return container.state.terminated.message
                    if container.last_state.terminated and container.last_state.terminated.message:
                        return container.last_state.terminated.message
            
            for container in pod.spec.containers:
                if container.name == container_name:
                    if container.termination_message_path:
                        return f"See termination log at {container.termination_message_path}"
            return "No termination message"
        except Exception:
            return "Failed to get termination message"

    def _check_probes(self, pod, container):
        """Check for probe failures"""
        if container.state.waiting and container.state.waiting.reason in [
            'CrashLoopBackOff', 
            'StartupProbeFailed'
        ]:
            return {
                "timestamp": datetime.now().isoformat(),
                "namespace": pod.metadata.namespace,
                "type": "PROBE_FAILURE",
                "pod": pod.metadata.name,
                "container": container.name,
                "reason": container.state.waiting.reason,
                "message": container.state.waiting.message,
                "workload": self._get_workload(pod),
                "resource_version": pod.metadata.resource_version,
                **{col: None for col in self.all_columns if col not in [
                    "timestamp", "namespace", "type", "pod", "container", 
                    "reason", "message", "workload", "resource_version"
                ]}
            }
        return None

    def _check_state_change(self, pod, container):
        """Detect container state changes"""
        current_state = self._get_container_state(container)
        state_id = f"{pod.metadata.uid}-{container.name}"
        
        if state_id in self.previous_states and self.previous_states[state_id] != current_state:
            change = {
                "timestamp": datetime.now().isoformat(),
                "namespace": pod.metadata.namespace,
                "type": "STATE_CHANGE",
                "pod": pod.metadata.name,
                "container": container.name,
                "from": self.previous_states[state_id],
                "to": current_state,
                "workload": self._get_workload(pod),
                "resource_version": pod.metadata.resource_version,
                **{col: None for col in self.all_columns if col not in [
                    "timestamp", "namespace", "type", "pod", "container", 
                    "from", "to", "workload", "resource_version"
                ]}
            }
            self.previous_states[state_id] = current_state
            return change
            
        self.previous_states[state_id] = current_state
        return None

    def _get_container_state(self, container):
        """Get human-readable container state"""
        if container.state.running:
            return "Running"
        elif container.state.waiting:
            return f"Waiting({container.state.waiting.reason})"
        elif container.state.terminated:
            return f"Terminated({container.state.terminated.reason})"
        return "Unknown"

    def run(self):
        """Main monitoring loop"""
    
        if self.args.workloads:
            print(f"   Specific workloads: {', '.join(self.args.workloads)}")

        
        # Initial sync
        for ns in self.args.namespaces:
            self._process_namespace(ns)
            
            if self.args.watch:
                self._start_watcher(ns)

        # Fine warm-up: pulisco tutti gli eventi già raccolti, 
        # così da partire “da zero” per gli alert
        self.recorded_events.clear()
        self.all_recent_events.clear()
        self._warmup = False
        print("✅ Warm-up completed, from now on only new events will alert.")
        
        # Main monitoring loop
        try:
            while True:
                if self.args.nodes:
                    self._check_nodes()
                time.sleep(INTERVAL_SEC)
                if int(time.time()) % (5 * 60) < INTERVAL_SEC:
                    generate_summary_csv(self.all_recent_events, self.args)
                    run_api_analysis(self.api_profiler.records, output_dir="api_analyzer")
        except KeyboardInterrupt:
            self._cleanup()
            print("\n🛑 Monitoring stopped")

    def _process_namespace(self, namespace):
        """Process all pods in a namespace"""
        try:
            pods = self.api_profiler.profile("list", "pods", namespace, lambda: self.v1.list_namespaced_pod(namespace))
            # Store the latest resource version
            self.resource_versions[namespace] = pods.metadata.resource_version
            
            debug_data = []
            
            for pod in pods.items:
                if not self._should_monitor(pod, namespace):
                    continue
                    
                debug_data.extend(self._process_pod(pod))
            
            self._output(debug_data, namespace)
            
        except ApiException as e:
            print(f"⚠️ API error in {namespace}: {e}")

    def _process_pod(self, pod):
        """Process a single pod and its containers"""
        debug_data = []
        if not pod.status.container_statuses:
            return debug_data
            
        for container in pod.status.container_statuses:
            # OOM Detection
            if (container.state.terminated and 
                container.state.terminated.reason == "OOMKilled"):
                debug_data.append({
                    "timestamp": datetime.now().isoformat(),
                    "namespace": pod.metadata.namespace,
                    "type": "OOM_KILLED",
                    "pod": pod.metadata.name,
                    "container": container.name,
                    "exit_code": container.state.terminated.exit_code,
                    "message": "OOMKilled",
                    "workload": self._get_workload(pod),
                    "resource_version": pod.metadata.resource_version,
                    **{col: None for col in self.all_columns if col not in [
                        "timestamp", "namespace", "type", "pod", "container", 
                        "exit_code", "message", "workload", "resource_version"
                    ]}
                })

            # Termination detection
            if self._has_termination(container):
                debug_data.append(self._create_debug_info(pod, container, "TERMINATION"))
            
            # Probe failures
            if self.args.probes and container.state.waiting:
                probe_data = self._check_probes(pod, container)
                if probe_data:
                    debug_data.append(probe_data)
                    
            # State changes
            if self.args.state_changes:
                state_change = self._check_state_change(pod, container)
                if state_change:
                    debug_data.append(state_change)
                    
        return debug_data

    def _create_debug_info(self, pod, container, event_type):
        """Create standardized debug information dictionary"""
        info = {
            "timestamp": datetime.now().isoformat(),
            "namespace": pod.metadata.namespace,
            "type": event_type,
            "pod": pod.metadata.name,
            "container": container.name,
            "workload": self._get_workload(pod),
            "resource_version": pod.metadata.resource_version,
            **{col: None for col in self.all_columns if col not in [
                "timestamp", "namespace", "type", "pod", "container", 
                "workload", "resource_version"
            ]}
        }
        
        if event_type == "TERMINATION":
            term = container.last_state.terminated
            info.update({
                "exit_code": term.exit_code,
                "reason": term.reason,
                "finished_at": term.finished_at.isoformat()
            })
            
            if self.args.messages:
                info["message"] = self._get_message(pod, container.name)
                
        return info

    def _start_watcher(self, namespace):
        """Start a watch stream for a namespace with retry logic"""
        # if namespace in self.watchers:
        #     return

        # if is_thread_running_for(namespace, self.watchers):
        #     return
            
        # Initialize resource version if not exists
        if namespace not in self.resource_versions:
            try:
                pods = self.v1.list_namespaced_pod(namespace)
                self.resource_versions[namespace] = pods.metadata.resource_version
            except Exception as e:
                print(f"⚠️ Failed to get initial resource version for {namespace}: {e}")
                self.resource_versions[namespace] = "0"
        
        w = watch.Watch()
        self.watchers[namespace] = w
        
        def watch_loop():
            current_delay = self.watch_retry_delay
            retry_count = 0  # <--- aggiunto contatore

            while True:
                try:
                    print(f"🔄 Starting watch for namespace {namespace} (resourceVersion: {self.resource_versions[namespace]})")

                    stream = w.stream(
                        self.v1.list_namespaced_pod,
                        namespace,
                        resource_version=self.resource_versions[namespace],
                        timeout_seconds=300
                    )

                    for event in stream:
                        try:
                            self.resource_versions[namespace] = event['object'].metadata.resource_version
                            if event['type'] in ('ADDED', 'MODIFIED', 'DELETED'):
                                pod = event['object']
                                if self._should_monitor(pod, namespace):
                                    self._handle_watch_event(event)
                            current_delay = self.watch_retry_delay
                            retry_count = 0  # <--- reset in caso di successo
                        except Exception as inner_e:
                            print(f"⚠️ Error processing event in {namespace}: {inner_e}")
                            continue

                except Exception as e:
                    print(f"⚠️ Watch connection error in {namespace}: {str(e)}")
                    print(f"⏳ Retrying in {current_delay} seconds...")

                    time.sleep(current_delay)
                    retry_count += 1  # <--- incremento al fallimento

                    # Se troppi retry consecutivi falliti, interrompi il watch
                    if retry_count >= 4:
                        print(f"❌ Too many failures in namespace {namespace}. Removing from monitoring.")
                        if namespace in self.watchers:
                            del self.watchers[namespace]
                        if namespace in self.args.namespaces:
                            self.args.namespaces.remove(namespace)
                        return  # esce dal ciclo e termina thread

                    current_delay = min(current_delay * self.backoff_factor, self.max_retry_delay)

                    # Prova ad aggiornare la resourceVersion
                    try:
                        pods = self.v1.list_namespaced_pod(namespace)
                        self.resource_versions[namespace] = pods.metadata.resource_version
                        print(f"🔄 Updated resource version for {namespace}: {self.resource_versions[namespace]}")
                    except Exception as version_e:
                        print(f"⚠️ Failed to update resource version: {version_e}")
                        continue


        threading.Thread(target=watch_loop, daemon=True).start()

    def _handle_watch_event(self, event):
        pod = event['object']
        pod_uid = pod.metadata.uid
        if event['type'] == 'DELETED':
            workload = self.pod_workloads.get(pod_uid, "Unknown")
        else:
            workload = self._get_workload(pod)
            self.pod_workloads[pod_uid] = workload  # cache workload info

        event_id = f"{pod_uid}-{event['type']}-{pod.metadata.resource_version}"
        if event_id in self.recorded_events:
            return
        self.recorded_events.add(event_id)

        debug_data = self._process_pod(pod)

        if event['type'] == 'DELETED':
            debug_data.append({
                "timestamp": datetime.now().isoformat(),
                "namespace": pod.metadata.namespace,
                "type": "POD_DELETED",
                "pod": pod.metadata.name,
                "workload": workload,
                "resource_version": pod.metadata.resource_version,
                **{col: None for col in self.all_columns if col not in [
                    "timestamp", "namespace", "type", "pod", "workload", "resource_version"
                ]}
            })

        self._output(debug_data, pod.metadata.namespace)

    def _parse_cpu(self, cpu_str):
        if cpu_str.endswith("n"):  # nanocores
            return float(cpu_str[:-1]) / 1e9
        if cpu_str.endswith("m"):  # millicores
            return float(cpu_str[:-1]) / 1000
        return float(cpu_str)

    def _parse_mem(self, mem_str):
        """Converte memoria da stringa Kubernetes (es. Mi, Gi, Ki) a MiB float"""
        import re

        units = {
            "Ki": 1 / 1024,
            "Mi": 1,
            "Gi": 1024,
            "Ti": 1024 * 1024,
            "Pi": 1024 * 1024 * 1024,
            "Ei": 1024 * 1024 * 1024 * 1024,
        }

        # Match tipo '128Mi', '2048Ki', '1Gi'
        match = re.match(r"^([0-9.]+)([a-zA-Z]+)?$", mem_str.strip())
        if not match:
            return 0  # fallback

        value, unit = match.groups()
        value = float(value)
        if not unit:
            # Se non c'è unità, assumiamo byte e convertiamo in MiB
            return value / (1024 * 1024)
        return value * units.get(unit, 1)


    def _check_nodes(self):
        """Controlla e registra dettagli delle risorse di ogni nodo, inclusi taints e condizioni"""
        from kubernetes.client import CustomObjectsApi

        try:
            nodes = self.api_profiler.profile("list", "nodes", "", lambda: self.v1.list_node())
            pods = self.v1.list_pod_for_all_namespaces()
            metrics = {}
            try:
                if self.metrics_available is None:
                    try:
                        metrics_api = client.CustomObjectsApi()
                        metrics_list = metrics_api.list_cluster_custom_object(
                            group="metrics.k8s.io", version="v1beta1", plural="nodes"
                        )
                        self.metrics_available = True
                        metrics = {item["metadata"]["name"]: item for item in metrics_list.get("items", [])}
                    except ApiException as e:
                        print(f"⚠️ Metrics-server API error ({e.status}): {e.reason}")
                        print(f"  → Response body: {e.body}")
                        self.metrics_available = False
                    except Exception as e:
                        # Non blocchiamo definitivamente su errori generici
                        print(f"⚠️ Unexpected error accessing metrics-server: {e}")
                        metrics = {}
                elif self.metrics_available is True:
                    metrics_api = client.CustomObjectsApi()
                    metrics_list = metrics_api.list_cluster_custom_object(
                        group="metrics.k8s.io", version="v1beta1", plural="nodes"
                    )
                    metrics = {item["metadata"]["name"]: item for item in metrics_list.get("items", [])}
            except Exception:
                pass

            current_time = datetime.now().isoformat()

            for node in nodes.items:
                node_name = node.metadata.name
                capacity = node.status.capacity
                allocatable = node.status.allocatable

                # Condizioni del nodo
                condition_map = {c.type: c.status for c in node.status.conditions or []}

                # Taints del nodo
                taints = node.spec.taints or []
                taint_summary = "; ".join([
                    f"{t.effect}:{t.key}={t.value}" if t.value else f"{t.effect}:{t.key}"
                    for t in taints
                ])

                # Calcolo richieste/limiti pod attivi
                node_pods = [p for p in pods.items if p.spec.node_name == node_name]

                cpu_req = cpu_lim = mem_req = mem_lim = 0
                for pod in node_pods:
                    for container in pod.spec.containers:
                        resources = container.resources
                        if resources.requests:
                            cpu_req += self._parse_cpu(resources.requests.get("cpu", "0"))
                            mem_req += self._parse_mem(resources.requests.get("memory", "0"))
                        if resources.limits:
                            cpu_lim += self._parse_cpu(resources.limits.get("cpu", "0"))
                            mem_lim += self._parse_mem(resources.limits.get("memory", "0"))

                usage_cpu = usage_mem = None
                if node_name in metrics:
                    usage_cpu = self._parse_cpu(metrics[node_name]["usage"]["cpu"])
                    usage_mem = self._parse_mem(metrics[node_name]["usage"]["memory"])

                
                for cond in ["MemoryPressure", "DiskPressure", "Ready", "NetworkUnavailable"]:
                    status = condition_map.get(cond)
                    # per “NotReady” usiamo Ready==False
                    if (cond == "Ready" and status == "False") or (cond != "Ready" and status == "True"):
                        evt = {
                            "timestamp": current_time,
                            "type":       "NotReady"         if cond=="Ready" else cond,
                            "node":        node_name,
                            "message":     f"{cond} status = {status}",
                        }
                        should, cfg = self.alert_manager.should_alert(evt)
                        if should:
                            self.alert_manager.send_teams_alert(evt, cfg)
                            if self.alert_manager.mail_config:
                                self.alert_manager.send_nodes_email_alert(evt, cfg)

                # NotSchedulable
                if getattr(node.spec, "unschedulable", False):
                    evt = {
                        "timestamp": current_time,
                        "type":       "NotSchedulable",
                        "node":        node_name,
                        "message":     "Node is cordoned (unschedulable)",
                    }
                    should, cfg = self.alert_manager.should_alert(evt)
                    if should:
                        self.alert_manager.send_teams_alert(evt, cfg)
                        if self.alert_manager.mail_config:
                            self.alert_manager.send_nodes_email_alert(evt, cfg)


                self._output_node_status({
                    "timestamp": current_time,
                    "type": "NODE_RESOURCE",
                    "node": node_name,
                    "cpu_capacity": self._parse_cpu(capacity.get("cpu", "0")),
                    "cpu_allocatable": self._parse_cpu(allocatable.get("cpu", "0")),
                    "cpu_requests": round(cpu_req, 2),
                    "cpu_limits": round(cpu_lim, 2),
                    "cpu_usage": round(usage_cpu, 2) if usage_cpu else None,
                    "mem_capacity": self._parse_mem(capacity.get("memory", "0")),
                    "mem_allocatable": self._parse_mem(allocatable.get("memory", "0")),
                    "mem_requests": round(mem_req, 2),
                    "mem_limits": round(mem_lim, 2),
                    "mem_usage": round(usage_mem, 2) if usage_mem else None,
                    "condition_Ready": condition_map.get("Ready"),
                    "condition_MemoryPressure": condition_map.get("MemoryPressure"),
                    "condition_DiskPressure": condition_map.get("DiskPressure"),
                    "condition_PIDPressure": condition_map.get("PIDPressure"),
                    "condition_NetworkUnavailable": condition_map.get("NetworkUnavailable"),
                    "taints": taint_summary,
                }, node_name)
        

                

        except Exception as e:
            print(f"⚠️ Node monitoring error: {e}")



    def _output_node_status(self, data, node_name):
        """Scrive i dati dei nodi in ./nodes/debug_node_<node>.csv"""
        import os
        import csv

        node_dir = os.path.join(os.getcwd(), "nodes")
        os.makedirs(node_dir, exist_ok=True)

        filename = os.path.join(node_dir, f"debug_node_{node_name}.csv")
        file_exists = os.path.isfile(filename)

        fieldnames = [
            "timestamp", "type", "node",
            "cpu_capacity", "cpu_allocatable", "cpu_usage", "cpu_requests", "cpu_limits",
            "mem_capacity", "mem_allocatable", "mem_usage", "mem_requests", "mem_limits",
            "condition_Ready", "condition_MemoryPressure", "condition_DiskPressure",
            "condition_PIDPressure", "condition_NetworkUnavailable",
            "taints"
        ]

        with open(filename, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            # scrive solo i campi previsti nei fieldnames
            filtered_data = {k: data.get(k) for k in fieldnames}
            writer.writerow(filtered_data)



    def _output(self, data, namespace):
        """Handle output to both CSV and log stream"""

        if self._warmup:
            return

        self.all_recent_events.extend(data)

        EVENT_PRIORITY = {
            "OOM_KILLED":       100,
            "ExitCode_137":     90,
            "TERMINATION":      80,
            "PROBE_FAILURE":    70,
            "CrashLoopBackOff": 60,
            "STATE_CHANGE":     10,
            # ... altri tipi se servono
        }

        if not data:
            return
            
        # Filter already processed data
        filtered_data = []
        for entry in data:
            # Create unique ID based on timestamp, pod, container and type
            entry_id = f"{entry.get('timestamp')}-{entry.get('pod')}-{entry.get('container', '')}-{entry.get('type')}"
            if entry_id not in self.recorded_events:
                self.recorded_events.add(entry_id)
                filtered_data.append(entry)
        
        if not filtered_data:
            return
                    
        # CSV output
        if self.args.csv:
            self._write_csv(filtered_data, namespace)

        teams_enabled = bool(self.alert_manager.teams_webhook_url)
        email_enabled = bool(self.alert_manager.mail_config)

        if not teams_enabled and not self._teams_warning_printed:
            print("⚠️ No Teams webhook configured.")
            self._teams_warning_printed = True
        if not email_enabled and not self._email_warning_printed:
            print("⚠️ No email config defined.")
            self._email_warning_printed = True
            

        # Send alerts
        best_events = {}
        for entry in filtered_data:
            pod = entry["pod"]
            container = entry.get("container", "")
            key = (pod, container)
            # prendi priorità, default 0 se non in dict
            prio = EVENT_PRIORITY.get(entry["type"], 0)
            # se non abbiamo ancora un evento, o questo ha priorità maggiore
            if key not in best_events or prio > EVENT_PRIORITY.get(best_events[key]["type"], 0):
                best_events[key] = entry

        # usa solo gli eventi selezionati
        filtered_data = list(best_events.values())


        for entry in filtered_data:
            should_alert, cfg = self.alert_manager.should_alert(entry)
            if not should_alert:
                continue
            if teams_enabled:
                self.alert_manager.send_teams_alert(entry, cfg)
            if email_enabled:
                self.alert_manager.send_email_alert(entry, cfg)

            
        # JSON log stream
        if self.args.service_account or self.args.logs:
            for entry in filtered_data:
                print(json.dumps(entry))


    def _write_csv(self, data, namespace):
        """Scrive i CSV per i workload nella cartella 'workload/' relativa alla working dir"""

        if not data:
            return

        workload_dir = os.path.join(os.getcwd(), "workload", namespace)
        os.makedirs(workload_dir, exist_ok=True)

        

        for entry in data:
            workload = str(entry.get("workload") or "unknown").replace("/", "_")
            filename = os.path.join(workload_dir, f"debug_{workload}.csv")
            file_exists = os.path.isfile(filename)


            with open(filename, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.all_columns)
                if not file_exists:
                    writer.writeheader()
                complete_entry = {col: entry.get(col, None) for col in self.all_columns}
                writer.writerow(complete_entry)


    def _cleanup(self):
        """Clean up resources before exit"""
        for watcher in self.watchers.values():
            watcher.stop()

def generate_summary_csv(events, args):
    summary = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.get("type") not in ["TERMINATION", "POD_DELETED"]:
            continue

        ns = e.get("namespace", "-")
        wl = e.get("workload", "Unknown")
        key = (ns, wl)

        if e.get("type") == "TERMINATION":
            summary[key]["TotalRestarts"] += 1
            summary[key]["Termination"] += 1
        elif e.get("type") == "POD_DELETED":
            summary[key]["TotalRestarts"] += 1
            summary[key]["Pod_Deleted"] += 1

        code = e.get("exit_code")
        if code is not None:
            summary[key][f"ExitCode_{code}"] += 1

    records = []
    for (ns, wl), data in summary.items():
        row = {"Namespace": ns, "Workload": wl}
        row["Termination"] = data.get("Termination", 0) > 0
        row["Pod_Deleted"] = data.get("Pod_Deleted", 0) > 0
        row.update(data)
        records.append(row)

    df = pd.DataFrame(records)

    if not records:
        print("📭 No restart or deletion events to summarize.")
        return

    if df.empty:
        print("📭 No restart or deletion events to summarize.")
        return

    # df = safe_sort_dataframe(df, "TotalRestarts")


    filename = "cluster_overview_chaos.csv" if args.chaos else "workload_overview.csv"
    df.to_csv(filename, index=False)
    print(f"📊 Summary (TERMINATION and POD_DELETED) written to {filename}")


def main():

    

    parser = argparse.ArgumentParser(description="Enhanced Kubernetes Pod Debugger")

    # Connection options
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--service-account', action='store_true',
                      help='Use in-cluster service account')
    group.add_argument('--context', type=str,
                      help='Kubeconfig context name')

    # Monitoring scope
    parser.add_argument('--namespaces', nargs='+', default=[DEFAULT_NAMESPACE],
                      help='Namespaces to monitor')
    parser.add_argument('--workloads', nargs='+',
                      help='Specific deployments/statefulsets to monitor')
    parser.add_argument('--chaos', action='store_true',
                      help='Enable CAOS mode: monitor all namespaces')

    # Monitoring features
    parser.add_argument('--watch', action='store_true',
                      help='Enable real-time watch API')
    parser.add_argument('--nodes', action='store_true',
                      help='Enable node monitoring')
    parser.add_argument('--probes', action='store_true',
                      help='Monitor probe failures')
    parser.add_argument('--state-changes', action='store_true',
                      help='Track container state changes')
    parser.add_argument('--messages', action='store_true',
                      help='Include termination messages')

    # Output options
    parser.add_argument('--csv', action='store_true', default=True,
                      help='Enable CSV output')
    parser.add_argument('--logs', action='store_true',
                      help='Enable JSON log streaming')

    args = parser.parse_args()

    

    if args.chaos:
        try:
            config.load_incluster_config() if args.service_account else config.load_kube_config(context=args.context)
            args.namespaces = [ns.metadata.name for ns in client.CoreV1Api().list_namespace().items]
            print("🚀 Chaos mode enabled: monitoring ALL namespaces")
        except Exception as e:
            print(f"❌ Failed to load namespaces for CAOS mode: {e}")
            return

    debugger = PodRestartDebugger(args)
    if debugger.setup_clients():
        if args.chaos:
            # 🔁 Avvia il watcher per aggiunta/rimozione namespace
            debugger._start_namespace_watcher()
        debugger.run()

if __name__ == "__main__":

    KU_BOG_ASCII = r"""
                   _______
                .-'       '-.
              .'             '.
             /    .------.     \
            /    /        \     \
            |   |   (•)    |    |
            |   |          |    |
            \    \        /     /
             \    '------'     /
              '.             .'
                '-._______.-'

                K U B O G  v1.0
                    """

    print(KU_BOG_ASCII)
    main()
