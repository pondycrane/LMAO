# local-path provisioner fix (2026-10-01)

## Symptom
New `local-path` PVCs stayed Pending with `ProvisioningFailed: … create process
timeout after 120 seconds`; the provisioner pod had ~3500 restarts.

## Root causes (three, stacked)
1. **Cluster can't pull Docker Hub images** (effectively air-gapped): the
   local-path helper (`rancher/mirrored-library-busybox:1.37.0`) could never
   start → volume-creation helper timed out.
2. **tp1 cannot reach the local registry** (`192.168.50.153:5000` — `network is
   unreachable`), so even after mirroring, the provisioner had to run on
   tp3/tp4. The provisioning deployment kept scheduling on tp1.
3. **`nodeName` in pod specs bypasses the scheduler** → WaitForFirstConsumer
   PVCs never get their selected-node annotation and never bind (must use
   nodeAffinity so the scheduler's volume binding still runs).

## Fix (applied to the cluster)
```
# mirror helper + provisioner images into the local registry (bare-metal box has Docker Hub)
docker pull rancher/mirrored-library-busybox:1.37.0
docker tag  rancher/mirrored-library-busybox:1.37.0 192.168.50.153:5000/rancher/mirrored-library-busybox:1.37.0
docker push 192.168.50.153:5000/rancher/mirrored-library-busybox:1.37.0
docker pull rancher/local-path-provisioner:v0.0.36
docker tag  rancher/local-path-provisioner:v0.0.36 192.168.50.153:5000/rancher/local-path-provisioner:v0.0.36
docker push 192.168.50.153:5000/rancher/local-path-provisioner:v0.0.36

# helperPod image -> local registry
kubectl patch cm local-path-config -n kube-system --type merge -p \
  '{"data":{"helperPod.yaml":"apiVersion: v1\nkind: Pod\nmetadata:\n  name: helper-pod\nspec:\n  containers:\n  - name: helper-pod\n    image: \"192.168.50.153:5000/rancher/mirrored-library-busybox:1.37.0\"\n    imagePullPolicy: IfNotPresent\n"}}'

# provisioner image -> local registry + pin to a registry-reachable node (tp4)
kubectl patch deployment -n kube-system local-path-provisioner --patch \
  '{"spec":{"template":{"spec":{"containers":[{"name":"local-path-provisioner","image":"192.168.50.153:5000/rancher/local-path-provisioner:v0.0.36","imagePullPolicy":"IfNotPresent"}]}}}}'
kubectl patch deployment -n kube-system local-path-provisioner --patch \
  '{"spec":{"template":{"spec":{"nodeName":"tp4"}}}}'
```

Note: k3s owns the `local-storage` addon; a k3s node restart may reconcile the
provisioner deployment back to the upstream manifest — re-apply the patches if
new PVCs start failing again.

## Verified
`lptest` PVC bound in ~25 s (helper ran, volume created on tp4). The Rust server
ContactBook now persists on `k8s/lmao-server-rust-app.yaml`'s PVC — a registered
contact survived a pod delete + rollout.
