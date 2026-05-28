#!/usr/bin/env python3
"""
add_variant.py — Add a secondary WVA variant to an existing single-stack benchmark.

Implements Topology B: one shared InferencePool/EPP fed by two Deployments,
each with its own VariantAutoscaling (VA) at a different variantCost. The WVA
saturation solver uses variantCost to decide which variant to scale first.

Label strategy
--------------
The InferencePool created by the primary standup selects pods by:
  llm-d.ai/inferenceServing: "true"   (camelCase)
  llm-d.ai/model:            <hash>

The primary Deployment selector additionally requires:
  llm-d.ai/inference-serving: "true"  (kebab-case)

The secondary Deployment this script creates:
  - KEEPS  llm-d.ai/inferenceServing + llm-d.ai/model  → joins the pool
  - OMITS  llm-d.ai/inference-serving (kebab)           → not claimed by primary
  - ADDS   wva.llmd.ai/variant: <suffix>                → unique selector

Both VAs share the same spec.modelID so the WVA solver groups them.

Usage
-----
  python hack/benchmark/add_variant.py -n NAMESPACE [--variant-suffix v2]
      [--variant-cost 5.0] [--min-replicas 1] [--max-replicas 10] [--dry-run]
"""

import argparse
import copy
import json
import subprocess
import sys


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
        import json as _j
        print("---")
        print(_j.dumps(obj, indent=2))
        return
    kubectl("apply", "-f", "-", stdin=payload)


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


def find_primary_deployment(namespace):
    # The llm-d.ai/* labels live on spec.selector.matchLabels (not metadata.labels),
    # so kubectl -l filtering doesn't work — fetch all Deployments and filter here.
    out = kubectl("get", "deployment", "-n", namespace, "-o", "json")
    items = json.loads(out)["items"]

    def _is_primary(d):
        sel = d.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if sel.get("llm-d.ai/inference-serving") != "true":
            return False
        if sel.get("llm-d.ai/role") != "decode":
            return False
        # Exclude secondary variants created by this script
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
        print(f"ERROR: Multiple primary deployments found: {names}. "
              "Specify --deployment-name to disambiguate.", file=sys.stderr)
        sys.exit(1)
    return primaries[0]


def find_primary_va(namespace, deployment_name):
    out = kubectl("get", "variantautoscaling", "-n", namespace, "-o", "json")
    vas = json.loads(out)["items"]
    for va in vas:
        if va["spec"]["scaleTargetRef"]["name"] == deployment_name:
            return va
    print(f"ERROR: No VariantAutoscaling found targeting deployment '{deployment_name}'",
          file=sys.stderr)
    sys.exit(1)


def find_primary_hpa(namespace, deployment_name):
    out = kubectl("get", "hpa", "-n", namespace, "-o", "json")
    hpas = json.loads(out)["items"]
    for hpa in hpas:
        if hpa["spec"]["scaleTargetRef"]["name"] == deployment_name:
            return hpa
    print(f"ERROR: No HPA found targeting deployment '{deployment_name}'",
          file=sys.stderr)
    sys.exit(1)


def make_secondary_deployment(primary, suffix, namespace):
    sec = copy.deepcopy(primary)
    _strip_managed(sec)

    primary_name = primary["metadata"]["name"]
    sec_name = f"{primary_name}-{suffix}"
    sec["metadata"]["name"] = sec_name
    sec["metadata"]["namespace"] = namespace

    spec = sec["spec"]
    spec["replicas"] = 1

    # --- pod template labels ------------------------------------------------
    tmpl_labels = spec["template"]["metadata"].setdefault("labels", {})
    # Remove kebab label so primary Deployment selector won't claim these pods
    tmpl_labels.pop("llm-d.ai/inference-serving", None)
    # Add variant discriminator
    tmpl_labels["wva.llmd.ai/variant"] = suffix
    # Override the WVA variant label inherited from the primary pod template so
    # this deployment's metrics map to the v2 VariantAutoscaling, not primary.
    # Required by PR #1145 (Prometheus relabeling -> llm_d_ai_variant).
    tmpl_labels["llm-d.ai/variant"] = sec_name

    # --- Deployment selector ------------------------------------------------
    # Must match the pod template labels (minus kebab, plus variant).
    # Kubernetes selector is immutable after creation so get it right once.
    sel = spec["selector"]["matchLabels"]
    sel.pop("llm-d.ai/inference-serving", None)
    sel["wva.llmd.ai/variant"] = suffix
    # Override inherited primary's llm-d.ai/variant value so this selector
    # matches the secondary's pod-template labels (PR #1145 alignment).
    sel["llm-d.ai/variant"] = sec_name

    return sec


def make_secondary_va(primary_va, sec_dep_name, suffix, namespace,
                      variant_cost, min_replicas, max_replicas):
    primary_name = primary_va["metadata"]["name"]
    sec = copy.deepcopy(primary_va)
    _strip_managed(sec)

    sec["metadata"]["name"] = f"{primary_name}-{suffix}"
    sec["metadata"]["namespace"] = namespace
    # Inherit controller-instance label so the namespace-scoped controller sees it
    sec["metadata"].setdefault("labels", {})
    sec["metadata"]["labels"]["wva.llmd.ai/controller-instance"] = namespace

    sec["spec"] = {
        "scaleTargetRef": {
            "kind": "Deployment",
            "name": sec_dep_name,
        },
        "modelID": primary_va["spec"]["modelID"],
        "variantCost": str(variant_cost),
        "minReplicas": min_replicas,
        "maxReplicas": max_replicas,
    }
    return sec


def make_secondary_hpa(primary_hpa, sec_dep_name, suffix, namespace,
                       min_replicas, max_replicas):
    primary_name = primary_hpa["metadata"]["name"]
    sec = copy.deepcopy(primary_hpa)
    _strip_managed(sec)

    sec["metadata"]["name"] = f"{primary_name}-{suffix}"
    sec["metadata"]["namespace"] = namespace

    sec["spec"]["scaleTargetRef"]["name"] = sec_dep_name
    sec["spec"]["minReplicas"] = min_replicas
    sec["spec"]["maxReplicas"] = max_replicas

    for m in sec["spec"].get("metrics", []):
        if m.get("type") == "External":
            sel = m["external"]["metric"]["selector"]["matchLabels"]
            if "variant_name" in sel:
                sel["variant_name"] = sec_dep_name

    return sec


def main():
    ap = argparse.ArgumentParser(
        description="Add a secondary WVA variant to an existing benchmark deployment."
    )
    ap.add_argument("-n", "--namespace", required=True,
                    help="Kubernetes namespace")
    ap.add_argument("--variant-suffix", default="v2",
                    help="Suffix appended to secondary resource names (default: v2)")
    ap.add_argument("--variant-cost", default="5.0",
                    help="variantCost for the secondary VariantAutoscaling (default: 5.0)")
    ap.add_argument("--min-replicas", type=int, default=1,
                    help="minReplicas for secondary VA and HPA (default: 1)")
    ap.add_argument("--max-replicas", type=int, default=10,
                    help="maxReplicas for secondary VA and HPA (default: 10)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print manifests as JSON without applying")
    args = ap.parse_args()

    ns = args.namespace
    suffix = args.variant_suffix

    print(f"[1/3] Finding primary decode Deployment in namespace '{ns}'...")
    primary_dep = find_primary_deployment(ns)
    dep_name = primary_dep["metadata"]["name"]
    model_hash = (primary_dep.get("spec", {}).get("selector", {})
                  .get("matchLabels", {}).get("llm-d.ai/model", "?"))
    print(f"      {dep_name}  (llm-d.ai/model={model_hash})")

    print(f"[2/3] Finding primary VariantAutoscaling...")
    primary_va = find_primary_va(ns, dep_name)
    model_id = primary_va["spec"]["modelID"]
    primary_cost = primary_va["spec"].get("variantCost", "?")
    print(f"      {primary_va['metadata']['name']}  "
          f"(modelID={model_id}, variantCost={primary_cost})")

    print(f"[3/3] Finding primary HPA...")
    primary_hpa = find_primary_hpa(ns, dep_name)
    print(f"      {primary_hpa['metadata']['name']}")

    sec_dep_name = f"{dep_name}-{suffix}"

    print(f"\nCreating secondary variant '{suffix}'  "
          f"variantCost={args.variant_cost}  modelID={model_id}\n")

    sec_dep = make_secondary_deployment(primary_dep, suffix, ns)
    sec_va = make_secondary_va(primary_va, sec_dep_name, suffix, ns,
                               args.variant_cost,
                               args.min_replicas, args.max_replicas)
    sec_hpa = make_secondary_hpa(primary_hpa, sec_dep_name, suffix, ns,
                                 args.min_replicas, args.max_replicas)

    for kind, obj in [("Deployment", sec_dep),
                      ("VariantAutoscaling", sec_va),
                      ("HPA", sec_hpa)]:
        name = obj["metadata"]["name"]
        print(f"  Applying {kind}: {name}")
        kubectl_apply(obj, dry_run=args.dry_run)

    if args.dry_run:
        return

    print()
    print("Secondary variant created successfully.")
    print(f"  Primary   (cost {primary_cost:>5}): {dep_name}")
    print(f"  Secondary (cost {args.variant_cost:>5}): {sec_dep_name}")
    print()
    print("Both VAs share modelID=" + repr(model_id) + ".")
    print("WVA will scale the cheaper variant first when both are saturated.")
    print()
    print("Verify:")
    print(f"  kubectl get va,hpa -n {ns}")
    print(f"  kubectl get pods -n {ns} "
          f"-l 'llm-d.ai/inferenceServing=true,llm-d.ai/model={model_hash}'")


if __name__ == "__main__":
    main()
