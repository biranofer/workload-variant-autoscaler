#!/usr/bin/env python3
"""
add_variant.py — Add a WVA variant to an existing single-stack benchmark.

Supports N variants. Run once per variant to add. Each variant gets its own
KEDA ScaledObject with the llm-d.ai/managed annotation that WVA discovers.

On the first run the script also creates a ScaledObject for the primary
deployment (if one does not already exist), converting any legacy direct HPA
that the benchmark harness may have created.

Implements Topology B: one shared InferencePool/EPP fed by two or more
Deployments, each with its own KEDA ScaledObject at a different
llm-d.ai/variant-cost. The WVA cost-aware optimizer scales the most efficient
variant (best capacity per unit cost) first and spills to the others.

Label strategy
--------------
The InferencePool created by the primary standup selects pods by:
  llm-d.ai/inferenceServing: "true"   (camelCase)
  llm-d.ai/model:            <hash>

The primary Deployment selector additionally requires:
  llm-d.ai/inference-serving: "true"  (kebab-case)

The variant Deployment this script creates:
  - KEEPS  llm-d.ai/inferenceServing + llm-d.ai/model  → joins the pool
  - OMITS  llm-d.ai/inference-serving (kebab)           → not claimed by primary
  - ADDS   wva.llmd.ai/variant: <suffix>                → unique selector

All ScaledObjects share the same llm-d.ai/model-id annotation so the WVA
solver groups them under one model and applies cost-weighted scaling.

Usage
-----
  python hack/benchmark/add_variant.py -n NAMESPACE \\
      --config hack/benchmark/scenarios/guides/variants/<name>.yaml \\
      --prometheus-url https://thanos-querier.openshift-monitoring.svc.cluster.local:9091

The variant config yaml declares only what differs from the primary:

  suffix: v2                     # required; added variant name suffix
  variantCost: "5.0"             # default "5.0"
  minReplicas: 1                 # default 1
  maxReplicas: 10                # default 10
  parallelism:
    tensor: 2                    # rewrites --tensor-parallel-size
  resources:
    nvidia.com/gpu: 2            # mirrors limits + requests on GPU containers
"""

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# kubectl helpers
# ---------------------------------------------------------------------------

def kubectl(*args, stdin=None, check=True):
    cmd = ["kubectl"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    if check and result.returncode != 0:
        print(f"ERROR: {' '.join(cmd)}\n{result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def kubectl_apply(obj, dry_run=False):
    payload = json.dumps(obj)
    if dry_run:
        print("---")
        print(json.dumps(obj, indent=2))
        return
    kubectl("apply", "-f", "-", stdin=payload)


def kubectl_delete(kind, name, namespace, dry_run=False):
    if dry_run:
        print(f"[dry-run] kubectl delete {kind} {name} -n {namespace}")
        return
    kubectl("delete", kind, name, "-n", namespace, "--ignore-not-found=true")


def _strip_managed(obj):
    """Remove server-managed fields before re-applying as a new object."""
    meta = obj.setdefault("metadata", {})
    for field in ("resourceVersion", "uid", "generation", "creationTimestamp",
                  "managedFields", "selfLink"):
        meta.pop(field, None)
    ann = meta.get("annotations", {})
    ann.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if not ann:
        meta.pop("annotations", None)
    obj.pop("status", None)
    tmpl_meta = obj.get("spec", {}).get("template", {}).get("metadata", {})
    tmpl_meta.pop("creationTimestamp", None)
    tmpl_meta.pop("annotations", None)
    return obj


# ---------------------------------------------------------------------------
# Variant config parsing
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "variantCost": "5.0",
    "minReplicas": 1,
    "maxReplicas": 10,
}


def load_variant_config(path):
    """Load a variant override yaml, validate, and apply defaults."""
    p = Path(path)
    if not p.is_file():
        print(f"ERROR: variant config not found: {p}", file=sys.stderr)
        sys.exit(1)
    try:
        cfg = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse {p}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(cfg, dict):
        print(f"ERROR: variant config {p} must be a yaml mapping, got "
              f"{type(cfg).__name__}", file=sys.stderr)
        sys.exit(1)
    if "suffix" not in cfg or not isinstance(cfg["suffix"], str) or not cfg["suffix"]:
        print(f"ERROR: variant config {p} must set non-empty 'suffix'",
              file=sys.stderr)
        sys.exit(1)
    for k, v in CONFIG_DEFAULTS.items():
        cfg.setdefault(k, v)
    cfg["variantCost"] = str(cfg["variantCost"])
    cfg["minReplicas"] = int(cfg["minReplicas"])
    cfg["maxReplicas"] = int(cfg["maxReplicas"])
    return cfg


# ---------------------------------------------------------------------------
# Resource discovery
# ---------------------------------------------------------------------------

def find_primary_deployment(namespace):
    out = kubectl("get", "deployment", "-n", namespace, "-o", "json")
    items = json.loads(out)["items"]

    def _is_primary(d):
        sel = d.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if sel.get("llm-d.ai/inference-serving") != "true":
            return False
        if sel.get("llm-d.ai/role") != "decode":
            return False
        if "wva.llmd.ai/variant" in sel:
            return False
        return True

    primaries = [d for d in items if _is_primary(d)]
    if not primaries:
        print("ERROR: No primary decode deployment found "
              "(spec.selector: llm-d.ai/inference-serving=true,llm-d.ai/role=decode)",
              file=sys.stderr)
        sys.exit(1)
    if len(primaries) > 1:
        names = [d["metadata"]["name"] for d in primaries]
        print(f"ERROR: Multiple primary deployments found: {names}.", file=sys.stderr)
        sys.exit(1)
    return primaries[0]


def find_managed_scaledobject(namespace, deployment_name):
    """Return the ScaledObject with llm-d.ai/managed=true targeting deployment_name, or None."""
    out = kubectl("get", "scaledobject", "-n", namespace, "-o", "json",
                  check=False)
    try:
        items = json.loads(out)["items"]
    except (json.JSONDecodeError, KeyError):
        return None
    for so in items:
        ann = so.get("metadata", {}).get("annotations", {})
        if ann.get("llm-d.ai/managed") != "true":
            continue
        if so.get("spec", {}).get("scaleTargetRef", {}).get("name") == deployment_name:
            return so
    return None


def find_managed_hpa(namespace, deployment_name):
    """Return the direct HPA with llm-d.ai/managed=true targeting deployment_name, or None."""
    out = kubectl("get", "hpa", "-n", namespace, "-o", "json")
    hpas = json.loads(out)["items"]
    for hpa in hpas:
        ann = hpa.get("metadata", {}).get("annotations", {})
        if ann.get("llm-d.ai/managed") != "true":
            continue
        if hpa.get("spec", {}).get("scaleTargetRef", {}).get("name") == deployment_name:
            return hpa
    return None


def detect_model_id(primary_deployment):
    """Extract the served model name from the primary deployment's vLLM args or env."""
    containers = (primary_deployment.get("spec", {})
                  .get("template", {}).get("spec", {})
                  .get("containers", []))
    for c in containers:
        # Path 1: --model flag in args
        args = c.get("args") or []
        for i, a in enumerate(args):
            if a == "--model" and i + 1 < len(args):
                return args[i + 1]
            if isinstance(a, str) and a.startswith("--model="):
                return a.split("=", 1)[1]
        # Path 2: VLLM_SERVED_MODEL_NAME or MODEL_NAME env var
        for e in (c.get("env") or []):
            if e.get("name") in ("VLLM_SERVED_MODEL_NAME", "MODEL_NAME", "LLMDBENCH_DEPLOY_CURRENT_MODEL"):
                v = e.get("value", "")
                if v:
                    return v
    return None


# ---------------------------------------------------------------------------
# Container-arg overrides (unchanged from original)
# ---------------------------------------------------------------------------

def _override_tensor_parallel(containers, tp_value):
    flag = "--tensor-parallel-size"
    target = str(tp_value)
    for c in containers:
        args = c.get("args")
        replaced_in_args = False
        if isinstance(args, list):
            new_args = []
            i = 0
            while i < len(args):
                a = args[i]
                if a == flag and i + 1 < len(args):
                    new_args.extend([flag, target])
                    i += 2
                    replaced_in_args = True
                elif isinstance(a, str) and a.startswith(flag + "="):
                    new_args.append(f"{flag}={target}")
                    i += 1
                    replaced_in_args = True
                else:
                    new_args.append(a)
                    i += 1
            c["args"] = new_args
        env = c.setdefault("env", [])
        replaced_in_env = False
        for e in env:
            if e.get("name") == "VLLM_TENSOR_PARALLELISM":
                e["value"] = target
                e.pop("valueFrom", None)
                replaced_in_env = True
                break
        if not replaced_in_env:
            env.append({"name": "VLLM_TENSOR_PARALLELISM", "value": target})
        if not replaced_in_args and not replaced_in_env:
            if not isinstance(args, list):
                args = []
                c["args"] = args
            args.extend([flag, target])


def _override_gpu_resources(containers, gpu_count):
    target = str(gpu_count)
    for c in containers:
        res = c.get("resources") or {}
        limits = res.get("limits") or {}
        requests = res.get("requests") or {}
        if "nvidia.com/gpu" not in limits and "nvidia.com/gpu" not in requests:
            continue
        limits["nvidia.com/gpu"] = target
        requests["nvidia.com/gpu"] = target
        res["limits"] = limits
        res["requests"] = requests
        c["resources"] = res


def _read_tensor_parallel(containers):
    flag = "--tensor-parallel-size"
    for c in containers:
        args = c.get("args") or []
        env = c.get("env") or []
        env_tp = next(
            (e.get("value") for e in env if e.get("name") == "VLLM_TENSOR_PARALLELISM"),
            None,
        )
        for i, a in enumerate(args):
            if a == flag and i + 1 < len(args):
                v = args[i + 1]
                if isinstance(v, str) and v.startswith("$"):
                    return env_tp
                return v
            if isinstance(a, str) and a.startswith(flag + "="):
                v = a.split("=", 1)[1]
                if v.startswith("$"):
                    return env_tp
                return v
        if env_tp is not None:
            return env_tp
    return None


def _read_gpu_per_pod(containers):
    for c in containers:
        res = c.get("resources") or {}
        for bucket in ("limits", "requests"):
            v = (res.get(bucket) or {}).get("nvidia.com/gpu")
            if v is not None:
                return v
    return None


def _all_containers(deployment):
    spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
    return list(spec.get("containers") or []) + list(spec.get("initContainers") or [])


# ---------------------------------------------------------------------------
# Object builders
# ---------------------------------------------------------------------------

def make_variant_deployment(primary, cfg, namespace):
    sec = copy.deepcopy(primary)
    _strip_managed(sec)

    suffix = cfg["suffix"]
    primary_name = primary["metadata"]["name"]
    sec_name = f"{primary_name}-{suffix}"
    sec["metadata"]["name"] = sec_name
    sec["metadata"]["namespace"] = namespace

    spec = sec["spec"]
    spec["replicas"] = 1

    tmpl_labels = spec["template"]["metadata"].setdefault("labels", {})
    tmpl_labels.pop("llm-d.ai/inference-serving", None)
    tmpl_labels["wva.llmd.ai/variant"] = suffix
    tmpl_labels["llm-d.ai/variant"] = sec_name

    sel = spec["selector"]["matchLabels"]
    sel.pop("llm-d.ai/inference-serving", None)
    sel["wva.llmd.ai/variant"] = suffix
    sel["llm-d.ai/variant"] = sec_name

    pod_spec = spec["template"]["spec"]
    main_containers = pod_spec.setdefault("containers", [])

    tp = (cfg.get("parallelism") or {}).get("tensor")
    if tp is not None:
        _override_tensor_parallel(main_containers, tp)

    gpu = (cfg.get("resources") or {}).get("nvidia.com/gpu")
    if gpu is not None:
        _override_gpu_resources(main_containers, gpu)

    return sec


def make_variant_scaledobject(dep_name, so_name, model_id, cost, min_replicas,
                               max_replicas, namespace, prometheus_url):
    """Build a KEDA ScaledObject for a WVA variant.

    WVA discovers the ScaledObject via the llm-d.ai/managed annotation and
    emits wva_desired_replicas{variant_name=<so_name>, namespace=<namespace>}.
    KEDA reads that metric and drives the HPA it manages.
    """
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "ScaledObject",
        "metadata": {
            "name": so_name,
            "namespace": namespace,
            "labels": {
                # Required for namespace-scoped WVA controller-instance filtering.
                "wva.llmd.ai/controller-instance": namespace,
            },
            "annotations": {
                "llm-d.ai/managed": "true",
                "llm-d.ai/model-id": model_id,
                "llm-d.ai/variant-cost": str(cost),
            },
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": dep_name,
            },
            "pollingInterval": 15,
            "cooldownPeriod": 300,
            "minReplicaCount": min_replicas,
            "maxReplicaCount": max_replicas,
            "fallback": {
                "failureThreshold": 3,
                "replicas": 1,
            },
            "advanced": {
                "restoreToOriginalReplicaCount": False,
                "horizontalPodAutoscalerConfig": {
                    "name": f"wva-keda-hpa-{dep_name}",
                    "behavior": {
                        "scaleUp": {
                            "stabilizationWindowSeconds": 0,
                            "policies": [
                                {"type": "Percent", "value": 100, "periodSeconds": 15},
                            ],
                        },
                        "scaleDown": {
                            "stabilizationWindowSeconds": 120,
                            "policies": [
                                {"type": "Percent", "value": 100, "periodSeconds": 15},
                            ],
                        },
                    },
                },
            },
            "triggers": [
                {
                    "type": "prometheus",
                    "name": "wva-desired-replicas",
                    "authenticationRef": {
                        "name": "wva-prometheus-auth",
                        "kind": "TriggerAuthentication",
                    },
                    "metadata": {
                        "serverAddress": prometheus_url,
                        "authModes": "bearer",
                        "query": (
                            f'wva_desired_replicas{{'
                            f'variant_name="{so_name}",'
                            f'namespace="{namespace}"'
                            f'}}'
                        ),
                        "threshold": "1",
                        "activationThreshold": "0",
                        "metricType": "Value",
                    },
                }
            ],
        },
    }


def make_trigger_authentication(namespace):
    """Build a KEDA TriggerAuthentication that borrows the WVA controller's SA token.

    The WVA ServiceAccount already holds cluster-monitoring-view, which is what
    Thanos Querier requires.  Without this, KEDA's Prometheus trigger gets 401
    and silently falls back to spec.fallback.replicas (=1), so scaling never fires.
    """
    return {
        "apiVersion": "keda.sh/v1alpha1",
        "kind": "TriggerAuthentication",
        "metadata": {
            "name": "wva-prometheus-auth",
            "namespace": namespace,
        },
        "spec": {
            "secretTargetRef": [
                {
                    "parameter": "bearerToken",
                    "name": "workload-variant-autoscaler-controller-manager-token",
                    "key": "token",
                },
                {
                    "parameter": "ca",
                    "name": "workload-variant-autoscaler-controller-manager-token",
                    "key": "service-ca.crt",
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Add a WVA variant to an existing benchmark deployment."
    )
    ap.add_argument("-n", "--namespace", required=True,
                    help="Kubernetes namespace")
    ap.add_argument("--config", required=True,
                    help="Path to a variant override yaml (see module docstring)")
    ap.add_argument("--prometheus-url",
                    default="https://thanos-querier.openshift-monitoring.svc.cluster.local:9091",
                    help="Prometheus server URL for KEDA triggers (default: OCP thanos-querier)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print manifests as JSON without applying")
    args = ap.parse_args()

    ns = args.namespace
    cfg = load_variant_config(args.config)
    suffix = cfg["suffix"]

    print(f"[1/4] Finding primary decode Deployment in namespace '{ns}'...")
    primary_dep = find_primary_deployment(ns)
    dep_name = primary_dep["metadata"]["name"]
    model_hash = (primary_dep.get("spec", {}).get("selector", {})
                  .get("matchLabels", {}).get("llm-d.ai/model", "?"))
    primary_containers = _all_containers(primary_dep)
    primary_tp = _read_tensor_parallel(primary_containers) or "1"
    primary_gpu = _read_gpu_per_pod(primary_containers) or "1"
    print(f"      {dep_name}  (llm-d.ai/model={model_hash})")

    print(f"[2/3] Ensuring TriggerAuthentication for Thanos Querier access...")
    trigger_auth = make_trigger_authentication(ns)
    kubectl_apply(trigger_auth, dry_run=args.dry_run)

    print(f"[3/4] Resolving primary ScaledObject...")
    primary_so = find_managed_scaledobject(ns, dep_name)
    primary_so_name = f"{dep_name}-scaler"

    if primary_so is None:
        # First run: check for legacy direct HPA left by the benchmark harness
        legacy_hpa = find_managed_hpa(ns, dep_name)
        if legacy_hpa is not None:
            hpa_ann = legacy_hpa.get("metadata", {}).get("annotations", {})
            model_id = hpa_ann.get("llm-d.ai/model-id") or detect_model_id(primary_dep)
            primary_cost = hpa_ann.get("llm-d.ai/variant-cost", "10.0")
            primary_min = legacy_hpa.get("spec", {}).get("minReplicas", 1)
            primary_max = legacy_hpa.get("spec", {}).get("maxReplicas", 10)
            print(f"      Found legacy direct HPA '{legacy_hpa['metadata']['name']}' "
                  f"(model-id={model_id}, cost={primary_cost}) — converting to ScaledObject")
        else:
            model_id = detect_model_id(primary_dep) or dep_name
            primary_cost = "10.0"
            primary_min = 1
            primary_max = 10
            print(f"      No ScaledObject or HPA found — creating primary ScaledObject "
                  f"(model-id={model_id}, cost={primary_cost})")

        primary_so_obj = make_variant_scaledobject(
            dep_name=dep_name,
            so_name=primary_so_name,
            model_id=model_id,
            cost=primary_cost,
            min_replicas=primary_min,
            max_replicas=primary_max,
            namespace=ns,
            prometheus_url=args.prometheus_url,
        )
        print(f"  Applying primary ScaledObject: {primary_so_name}")
        kubectl_apply(primary_so_obj, dry_run=args.dry_run)

        if legacy_hpa is not None and not args.dry_run:
            hpa_name = legacy_hpa["metadata"]["name"]
            print(f"  Deleting legacy direct HPA: {hpa_name}")
            kubectl_delete("hpa", hpa_name, ns, dry_run=args.dry_run)
    else:
        ann = primary_so.get("metadata", {}).get("annotations", {})
        model_id = ann.get("llm-d.ai/model-id") or detect_model_id(primary_dep) or dep_name
        primary_cost = ann.get("llm-d.ai/variant-cost", "10.0")
        primary_so_name = primary_so["metadata"]["name"]
        print(f"      {primary_so_name}  (model-id={model_id}, cost={primary_cost})")

    print(f"[4/4] Creating variant '{suffix}'  "
          f"variantCost={cfg['variantCost']}  modelID={model_id}")

    var_dep_name = f"{dep_name}-{suffix}"
    var_so_name = f"{var_dep_name}-scaler"

    var_dep = make_variant_deployment(primary_dep, cfg, ns)
    var_so = make_variant_scaledobject(
        dep_name=var_dep_name,
        so_name=var_so_name,
        model_id=model_id,
        cost=cfg["variantCost"],
        min_replicas=cfg["minReplicas"],
        max_replicas=cfg["maxReplicas"],
        namespace=ns,
        prometheus_url=args.prometheus_url,
    )

    print(f"  Applying Deployment: {var_dep_name}")
    kubectl_apply(var_dep, dry_run=args.dry_run)

    # Owner refs on ScaledObject point to the variant Deployment so that
    # deleting the Deployment also garbage-collects the ScaledObject.
    # Must read the UID after applying the Deployment.
    if not args.dry_run:
        var_dep_uid = json.loads(kubectl(
            "get", "deployment", var_dep_name, "-n", ns, "-o", "json",
        ))["metadata"]["uid"]
        owner_ref = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": var_dep_name,
            "uid": var_dep_uid,
            "blockOwnerDeletion": True,
            "controller": False,
        }
        var_so.setdefault("metadata", {}).setdefault("ownerReferences", []).append(owner_ref)

    print(f"  Applying ScaledObject: {var_so_name}")
    kubectl_apply(var_so, dry_run=args.dry_run)

    if args.dry_run:
        return

    var_containers = _all_containers(var_dep)
    var_tp = _read_tensor_parallel(var_containers) or "1"
    var_gpu = _read_gpu_per_pod(var_containers) or "1"

    print()
    print("Variant added successfully.")
    print(f"  Primary (cost {primary_cost:>5}, TP={primary_tp}, "
          f"{primary_gpu} GPU/pod): {dep_name}")
    print(f"  Added   (cost {cfg['variantCost']:>5}, TP={var_tp}, "
          f"{var_gpu} GPU/pod): {var_dep_name}")
    print()
    print("All ScaledObjects share model-id=" + repr(model_id) + ".")
    print("WVA scales the most efficient variant first (highest capacity per")
    print("unit cost), spilling to the others once it saturates.")
    print()
    print("Verify:")
    print(f"  kubectl get scaledobject,hpa -n {ns}")
    print(f"  kubectl get pods -n {ns} "
          f"-l 'llm-d.ai/inferenceServing=true,llm-d.ai/model={model_hash}'")


if __name__ == "__main__":
    main()
