#!/usr/bin/env bash
#
# Tap Kubernetes pod traffic in an EKS cluster with CloudLens sensors,
# end to end: cluster discovery or creation, access verification, sensor
# image supply, DaemonSet (or sidecar) deployment, a sample traffic app,
# and verification. Everything is resolved at run time from the flags and
# the live account; nothing about a customer is hardcoded.
#
# Why the shapes it has:
#   - DaemonSet is the automated path: one privileged pod per node taps
#     every pod on that node (Keysight vTAP UG 913-2798-01, "CloudLens
#     DaemonSet in Kubernetes"). One object, no customer pod restarts.
#   - Sidecar is per-pod: the sensor container is added INSIDE each
#     tapped pod's spec (vTAP UG, "Sidecar Deployment in Kubernetes").
#     Injecting containers into a customer's running Deployments restarts
#     their pods, so for customer apps this script renders a ready-to-
#     paste sidecar block and tells the operator exactly where it goes;
#     it only auto-injects into the SAMPLE app, which is ours to restart.
#   - kubectl, not helm: AWS CloudShell preinstalls kubectl and not helm.
#     The DaemonSet manifest is Keysight's own chart pre-rendered with
#     helm template (deploy/kubernetes/cloudlens-sensor-daemonset.yaml),
#     substituted here. Chart source: the published repo
#     https://keysight-tech.github.io/cloudlens-helm/
#   - The sensor image must be in a registry the NODES can pull from
#     (vTAP UG: "you must upload the sensor Docker image to the Image
#     Registry before the Pod is created"). On EKS that is ECR; this
#     script creates the repo and pushes a local sensor tar when needed.
#
# Usage (standalone; deploy-stack.sh drives it with the same flags):
#   bash scripts/deploy-eks-tapping.sh \
#     --region us-east-1 --clms-ip 10.0.0.10 --project-key <key> \
#     [--cluster my-eks | --create-sample --vpc-id vpc-... ] \
#     [--mode daemonset|sidecar] [--sensor-image <ecr-uri:tag>] \
#     [--sensor-tar ~/Downloads/CloudLens-Sensor-6.13.0-359.tar] \
#     [--sample-app] [--stack-name NAME] [--custom-tags k=v,...] [--yes]
#
# Exit codes: 0 done; 2 bad input; 3 no cluster access; 4 no sensor
# image; 5 cluster/nodegroup creation failed; 6 deployment failed.
set -uo pipefail

REGION=""
CLUSTER=""
CREATE_SAMPLE=false
VPC_ID=""
SUBNET_IDS=""
CLMS_IP=""
PROJECT_KEY=""
MODE="daemonset"
SENSOR_IMAGE=""
SENSOR_TAR=""
SAMPLE_APP=false
STACK_NAME="cloudlens"
CUSTOM_TAGS=""
ASSUME_YES=false
NAMESPACE="cloudlens"
NODE_TYPE="t3.medium"
NODE_COUNT=2

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)        REGION="$2"; shift 2 ;;
    --cluster)       CLUSTER="$2"; shift 2 ;;
    --create-sample) CREATE_SAMPLE=true; shift ;;
    --vpc-id)        VPC_ID="$2"; shift 2 ;;
    --subnet-ids)    SUBNET_IDS="$2"; shift 2 ;;
    --clms-ip)       CLMS_IP="$2"; shift 2 ;;
    --project-key)   PROJECT_KEY="$2"; shift 2 ;;
    --mode)          MODE="$(echo "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --sensor-image)  SENSOR_IMAGE="$2"; shift 2 ;;
    --sensor-tar)    SENSOR_TAR="$2"; shift 2 ;;
    --sample-app)    SAMPLE_APP=true; shift ;;
    --stack-name)    STACK_NAME="$2"; shift 2 ;;
    --custom-tags)   CUSTOM_TAGS="$2"; shift 2 ;;
    --namespace)     NAMESPACE="$2"; shift 2 ;;
    --node-type)     NODE_TYPE="$2"; shift 2 ;;
    --node-count)    NODE_COUNT="$2"; shift 2 ;;
    -y|--yes)        ASSUME_YES=true; shift ;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_BLUE='\033[0;34m'; C_RESET='\033[0m'
ok()   { echo -e "${C_GREEN}[ok]${C_RESET} $1"; }
warn() { echo -e "${C_YELLOW}[warn]${C_RESET} $1"; }
fail() { echo -e "${C_RED}[x]${C_RESET} $1" >&2; exit "${2:-2}"; }
step() { echo; echo -e "${C_BLUE}--- $1 ---${C_RESET}"; }
note() { echo "  -> $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DS_TEMPLATE="$REPO_ROOT/deploy/kubernetes/cloudlens-sensor-daemonset.yaml"

[[ -n "$REGION" ]]      || fail "--region is required"
[[ -n "$CLMS_IP" ]]     || fail "--clms-ip is required (the vController the sensors register to)"
[[ -n "$PROJECT_KEY" ]] || fail "--project-key is required (vController project page shows it)"
case "$MODE" in daemonset|sidecar) ;; *) fail "--mode must be daemonset or sidecar" ;; esac
[[ -f "$DS_TEMPLATE" ]] || fail "missing $DS_TEMPLATE (run from a repo checkout)"

AWS=(aws --region "$REGION")
command -v aws >/dev/null 2>&1 || fail "AWS CLI not found"

# kubectl: preinstalled in CloudShell; anywhere else, say exactly what to do
# rather than silently curl a binary onto the machine.
if ! command -v kubectl >/dev/null 2>&1; then
  fail "kubectl is not installed. AWS CloudShell has it preinstalled; on this
machine install it first (https://kubernetes.io/docs/tasks/tools/) and re-run." 2
fi

ACCOUNT=$("${AWS[@]}" sts get-caller-identity --query Account --output text 2>/dev/null) \
  || fail "AWS auth missing/expired. Run: aws sso login (or aws configure)"

# =====================================================================
# 1. The cluster: an existing one (discovered or named) or a sample.
# =====================================================================
step "Cluster"
if [[ "$CREATE_SAMPLE" != "true" && -z "$CLUSTER" ]]; then
  clusters=$("${AWS[@]}" eks list-clusters --query 'clusters[]' --output text 2>/dev/null | tr '\t' '\n')
  if [[ -z "$clusters" ]]; then
    fail "No EKS clusters in ${REGION} and --create-sample was not given.
Re-run with --create-sample to build a small test cluster (2x ${NODE_TYPE},
about 15 minutes), or --cluster <name> for a cluster in another profile." 2
  fi
  if [[ $(echo "$clusters" | wc -l) -eq 1 ]]; then
    CLUSTER="$clusters"
    ok "One EKS cluster in ${REGION}: ${CLUSTER}"
  elif [[ "$ASSUME_YES" == "true" || ! -t 0 ]]; then
    fail "Multiple EKS clusters in ${REGION}; pass --cluster <name>:
$(echo "$clusters" | sed 's/^/  /')" 2
  else
    echo "  EKS clusters in ${REGION}:"
    i=0; rows=()
    while IFS= read -r c; do i=$((i+1)); rows+=("$c"); echo "    $i) $c"; done <<< "$clusters"
    read -rp "  Choose 1-$i: " pick || true
    [[ "$pick" =~ ^[0-9]+$ ]] && (( pick >= 1 && pick <= i )) || fail "not a listed cluster" 2
    CLUSTER="${rows[$((pick-1))]}"
  fi
fi

if [[ "$CREATE_SAMPLE" == "true" ]]; then
  CLUSTER="${CLUSTER:-${STACK_NAME}-eks}"
  step "Creating the sample EKS cluster '${CLUSTER}' (~15 minutes)"
  [[ -n "$VPC_ID" ]] || fail "--create-sample needs --vpc-id (the stack's VPC)" 2

  # EKS demands subnets in at least two AZs; the stack's VPC is single-AZ by
  # design, so a second-AZ subnet is added (tagged so teardown attributes it).
  if [[ -z "$SUBNET_IDS" ]]; then
    mapfile_subnets=$("${AWS[@]}" ec2 describe-subnets \
      --filters "Name=vpc-id,Values=${VPC_ID}" \
      --query 'Subnets[].[SubnetId,AvailabilityZone]' --output text 2>/dev/null)
    first_subnet=$(echo "$mapfile_subnets" | head -1 | cut -f1)
    first_az=$(echo "$mapfile_subnets" | head -1 | cut -f2)
    second_subnet=$(echo "$mapfile_subnets" | awk -v az="$first_az" '$2 != az {print $1; exit}')
    if [[ -z "$second_subnet" ]]; then
      other_az=$("${AWS[@]}" ec2 describe-availability-zones \
        --query 'AvailabilityZones[?ZoneName!=`'"$first_az"'`].ZoneName | [0]' --output text)
      vpc_cidr=$("${AWS[@]}" ec2 describe-vpcs --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock' --output text)
      # carve an unused /24 high in the VPC range
      base="${vpc_cidr%.*.*}"
      new_cidr="${base}.250.0/24"
      note "The VPC is single-AZ; EKS needs two. Adding ${new_cidr} in ${other_az}."
      second_subnet=$("${AWS[@]}" ec2 create-subnet --vpc-id "$VPC_ID" \
        --cidr-block "$new_cidr" --availability-zone "$other_az" \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${STACK_NAME}-eks-az2},{Key=cloudlens:stack,Value=${STACK_NAME}}]" \
        --query 'Subnet.SubnetId' --output text) || fail "could not create the second-AZ subnet" 5
      rt=$("${AWS[@]}" ec2 describe-route-tables \
        --filters "Name=association.subnet-id,Values=${first_subnet}" \
        --query 'RouteTables[0].RouteTableId' --output text)
      [[ -n "$rt" && "$rt" != "None" ]] && "${AWS[@]}" ec2 associate-route-table \
        --route-table-id "$rt" --subnet-id "$second_subnet" >/dev/null 2>&1
      "${AWS[@]}" ec2 modify-subnet-attribute --subnet-id "$second_subnet" --map-public-ip-on-launch >/dev/null 2>&1
    fi
    SUBNET_IDS="${first_subnet},${second_subnet}"
  fi
  note "Cluster subnets: ${SUBNET_IDS}"

  cluster_role="${STACK_NAME}-eks-cluster-role"
  node_role="${STACK_NAME}-eks-node-role"
  if ! "${AWS[@]}" iam get-role --role-name "$cluster_role" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-role --role-name "$cluster_role" \
      --tags "Key=cloudlens:stack,Value=${STACK_NAME}" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"eks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null \
      || fail "cannot create the EKS cluster role (needs iam:CreateRole)" 5
    "${AWS[@]}" iam attach-role-policy --role-name "$cluster_role" \
      --policy-arn arn:aws:iam::aws:policy/AmazonEKSClusterPolicy >/dev/null
  fi
  if ! "${AWS[@]}" iam get-role --role-name "$node_role" >/dev/null 2>&1; then
    "${AWS[@]}" iam create-role --role-name "$node_role" \
      --tags "Key=cloudlens:stack,Value=${STACK_NAME}" \
      --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null \
      || fail "cannot create the EKS node role" 5
    for pol in AmazonEKSWorkerNodePolicy AmazonEKS_CNI_Policy AmazonEC2ContainerRegistryReadOnly; do
      "${AWS[@]}" iam attach-role-policy --role-name "$node_role" \
        --policy-arn "arn:aws:iam::aws:policy/${pol}" >/dev/null
    done
    sleep 10  # IAM eventual consistency before EKS validates the role
  fi

  if ! "${AWS[@]}" eks describe-cluster --name "$CLUSTER" >/dev/null 2>&1; then
    "${AWS[@]}" eks create-cluster --name "$CLUSTER" \
      --role-arn "arn:aws:iam::${ACCOUNT}:role/${cluster_role}" \
      --resources-vpc-config "subnetIds=${SUBNET_IDS}" \
      --tags "cloudlens:stack=${STACK_NAME}" >/dev/null \
      || fail "eks create-cluster failed (quota? role propagation? see the error above)" 5
  fi
  note "Waiting for the control plane (8-12 min is normal)..."
  "${AWS[@]}" eks wait cluster-active --name "$CLUSTER" || fail "cluster never became active" 5
  ok "Control plane active."

  if ! "${AWS[@]}" eks describe-nodegroup --cluster-name "$CLUSTER" --nodegroup-name "${CLUSTER}-nodes" >/dev/null 2>&1; then
    "${AWS[@]}" eks create-nodegroup --cluster-name "$CLUSTER" \
      --nodegroup-name "${CLUSTER}-nodes" \
      --node-role "arn:aws:iam::${ACCOUNT}:role/${node_role}" \
      --subnets ${SUBNET_IDS//,/ } \
      --instance-types "$NODE_TYPE" \
      --scaling-config "minSize=1,maxSize=${NODE_COUNT},desiredSize=${NODE_COUNT}" \
      --tags "cloudlens:stack=${STACK_NAME}" >/dev/null \
      || fail "eks create-nodegroup failed" 5
  fi
  note "Waiting for the nodes (2-5 min)..."
  "${AWS[@]}" eks wait nodegroup-active --cluster-name "$CLUSTER" --nodegroup-name "${CLUSTER}-nodes" \
    || fail "nodegroup never became active" 5
  ok "Nodes ready."
fi

# =====================================================================
# 2. kubectl access, verified before anything is applied.
# =====================================================================
step "Cluster access"
KCFG="$HOME/.kube/cloudlens-eks-${CLUSTER}"
mkdir -p "$HOME/.kube"
# A private kubeconfig: this deploy never rewrites the customer's default
# context.
"${AWS[@]}" eks update-kubeconfig --name "$CLUSTER" --kubeconfig "$KCFG" --alias "$CLUSTER" >/dev/null \
  || fail "update-kubeconfig failed for ${CLUSTER}" 3
K=(kubectl --kubeconfig "$KCFG")

probe_out=$("${K[@]}" get --raw=/version --request-timeout=20s 2>&1)
if [[ $? -ne 0 ]]; then
  case "$probe_out" in
    *Unauthorized*|*"must be logged in"*)
      caller_arn=$("${AWS[@]}" sts get-caller-identity --query Arn --output text)
      role_arn=$(sed -E 's|^arn:aws:sts::([0-9]+):assumed-role/([^/]+)/.*$|arn:aws:iam::\1:role/\2|' <<<"$caller_arn")
      fail "AWS lets you DESCRIBE ${CLUSTER}, but the cluster itself does not map
your principal (${caller_arn}). Whoever administers it must grant access:
  aws eks create-access-entry --cluster-name ${CLUSTER} --region ${REGION} \\
      --principal-arn ${role_arn}
  aws eks associate-access-policy --cluster-name ${CLUSTER} --region ${REGION} \\
      --principal-arn ${role_arn} \\
      --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \\
      --access-scope type=cluster
(on an older aws-auth ConfigMap cluster: add the principal to aws-auth instead)" 3 ;;
    *)
      fail "Cannot reach the ${CLUSTER} API endpoint: ${probe_out}
If the endpoint is private-only, run this from inside that VPC (or enable
public access on the cluster endpoint)." 3 ;;
  esac
fi
if ! "${K[@]}" auth can-i create daemonsets -A >/dev/null 2>&1; then
  fail "Authenticated to ${CLUSTER} but not allowed to create DaemonSets.
Ask the cluster admin for a role that can manage workloads cluster-wide." 3
fi
nodes=$("${K[@]}" get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
ok "Authenticated to ${CLUSTER}: ${nodes} node(s), DaemonSet rights confirmed."

# =====================================================================
# 3. The sensor image, in a registry the NODES can pull from.
# =====================================================================
step "Sensor image"
if [[ -z "$SENSOR_IMAGE" ]]; then
  ECR_REPO="cloudlens-sensor"
  ECR_URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}"
  tag=$("${AWS[@]}" ecr list-images --repository-name "$ECR_REPO" \
        --query 'imageIds[].imageTag' --output text 2>/dev/null | tr '\t' '\n' | sort -V | tail -1)
  if [[ -n "$tag" && "$tag" != "None" ]]; then
    SENSOR_IMAGE="${ECR_URI}:${tag}"
    ok "Using the sensor image already in ECR: ${SENSOR_IMAGE}"
  else
    # No image in ECR: push a local sensor tar (the download every CloudLens
    # entitlement includes). Auto-found so a customer who saved it anywhere
    # obvious does nothing extra.
    if [[ -z "$SENSOR_TAR" ]]; then
      SENSOR_TAR=$(ls -t "$HOME"/Downloads/CloudLens-Sensor-*.tar ./CloudLens-Sensor-*.tar 2>/dev/null | head -1)
    fi
    if [[ -z "$SENSOR_TAR" || ! -f "$SENSOR_TAR" ]]; then
      fail "No sensor image available. One of:
  --sensor-image <uri:tag>     an image already in a registry the nodes reach
  --sensor-tar <path>          the CloudLens-Sensor-<ver>.tar from Keysight
                               (support.ixiacom.com downloads; ~900MB)
The tar is loaded and pushed to ECR ${ECR_URI} automatically." 4
    fi
    command -v docker >/dev/null 2>&1 || fail "docker is needed to push ${SENSOR_TAR} to ECR (CloudShell has it)" 4
    ver=$(basename "$SENSOR_TAR" | sed -E 's/CloudLens-Sensor-(.+)\.tar/\1/')
    note "Pushing ${SENSOR_TAR} (version ${ver}) to ${ECR_URI} ..."
    "${AWS[@]}" ecr describe-repositories --repository-names "$ECR_REPO" >/dev/null 2>&1 \
      || "${AWS[@]}" ecr create-repository --repository-name "$ECR_REPO" \
           --tags "Key=cloudlens:stack,Value=${STACK_NAME}" >/dev/null
    "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null \
      || fail "docker login to ECR failed" 4
    loaded=$(docker load -i "$SENSOR_TAR" 2>/dev/null | sed -n 's/^Loaded image: //p' | head -1)
    [[ -n "$loaded" ]] || fail "docker load produced no image from ${SENSOR_TAR}" 4
    docker tag "$loaded" "${ECR_URI}:${ver}" || fail "docker tag failed" 4
    docker push "${ECR_URI}:${ver}" >/dev/null || fail "docker push failed" 4
    SENSOR_IMAGE="${ECR_URI}:${ver}"
    ok "Sensor image pushed: ${SENSOR_IMAGE}"
  fi
else
  ok "Using the supplied sensor image: ${SENSOR_IMAGE}"
fi
IMAGE_REPO="${SENSOR_IMAGE%:*}"
IMAGE_TAG="${SENSOR_IMAGE##*:}"

# =====================================================================
# 4. Deploy: DaemonSet (automated) or sidecar (sample auto, customer guided)
# =====================================================================
"${K[@]}" get namespace "$NAMESPACE" >/dev/null 2>&1 || "${K[@]}" create namespace "$NAMESPACE" >/dev/null

sidecar_block() {
  # The documented sidecar container (vTAP UG "Sidecar Deployment"), with
  # this run's real values. Indented for a containers: list.
  cat <<SIDE
        - name: cloudlens-sensor
          image: ${SENSOR_IMAGE}
          args: ["--auto_update","n","--accept_eula","yes","--ssl_verify","no",
                 "--server","${CLMS_IP}","--project_key","${PROJECT_KEY}"${CUSTOM_TAGS:+,
                 \"--custom_tags\",\"${CUSTOM_TAGS}\"}]
          securityContext:
            allowPrivilegeEscalation: true
            capabilities:
              add: ["SYS_MODULE","SYS_RESOURCE","NET_RAW","NET_ADMIN"]
          volumeMounts:
          - {name: host-root, mountPath: /host}
          - {name: host-log, mountPath: /var/log/cloudlens}
          - {name: lib-modules, mountPath: /lib/modules}
SIDE
}
sidecar_volumes() {
  cat <<VOLS
      - {name: host-root, hostPath: {path: /}}
      - {name: host-log, hostPath: {path: /var/log}}
      - {name: lib-modules, hostPath: {path: /lib/modules}}
VOLS
}

if [[ "$MODE" == "daemonset" ]]; then
  step "Deploying the CloudLens sensor DaemonSet"
  sed -e "s|__IMAGE_REPO__|${IMAGE_REPO}|g" \
      -e "s|__IMAGE_TAG__|${IMAGE_TAG}|g" \
      -e "s|__CLMS_IP__|${CLMS_IP}|g" \
      -e "s|__PROJECT_KEY__|${PROJECT_KEY}|g" \
      "$DS_TEMPLATE" | "${K[@]}" -n "$NAMESPACE" apply -f - \
    || fail "kubectl apply of the DaemonSet failed" 6
  note "Waiting for the sensor pods (image pull is ~1GB per node on first run)..."
  if ! "${K[@]}" -n "$NAMESPACE" rollout status ds/cloudlens-sensor --timeout=420s; then
    "${K[@]}" -n "$NAMESPACE" get pods -o wide || true
    fail "The sensor DaemonSet did not become ready. Common causes: the nodes
cannot pull ${SENSOR_IMAGE} (node role needs AmazonEC2ContainerRegistryReadOnly),
or a PodSecurity policy blocks privileged pods in namespace ${NAMESPACE}." 6
  fi
  ready=$("${K[@]}" -n "$NAMESPACE" get ds cloudlens-sensor -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}')
  ok "Sensor DaemonSet ready on ${ready} node(s)."
else
  step "Sidecar mode"
  snippet="${REPO_ROOT}/inventory/generated.eks-sidecar-${CLUSTER}.yaml"
  { echo "# CloudLens sidecar for cluster ${CLUSTER}, generated $(date -u +%FT%TZ)"
    echo "# 1. Add this container to each Deployment you want tapped, under"
    echo "#    spec.template.spec.containers:"
    sidecar_block
    echo "# 2. And these volumes under spec.template.spec.volumes:"
    sidecar_volumes
    echo "# 3. kubectl apply the changed Deployment; its pods restart with the"
    echo "#    sensor inside and register to ${CLMS_IP} within a minute."
  } > "$snippet"
  ok "Sidecar block written to ${snippet} with this run's live values."
  note "Customer pods are NEVER modified automatically: adding a sidecar"
  note "restarts them, and that call belongs to whoever owns the app."
fi

# =====================================================================
# 5. Sample traffic app (with the sidecar inlined when that mode is on)
# =====================================================================
if [[ "$SAMPLE_APP" == "true" ]]; then
  step "Sample traffic app (web x2 + loadgen, namespace cloudlens-demo)"
  extra_containers=""; extra_volumes=""
  if [[ "$MODE" == "sidecar" ]]; then
    extra_containers="$(sidecar_block)"
    extra_volumes="$(sidecar_volumes)"
  fi
  "${K[@]}" apply -f - <<APP || fail "sample app apply failed" 6
apiVersion: v1
kind: Namespace
metadata:
  name: cloudlens-demo
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  namespace: cloudlens-demo
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
        - name: nginx
          image: public.ecr.aws/nginx/nginx:stable
          ports: [{containerPort: 80}]
${extra_containers}
      volumes:
${extra_volumes:-        []}
---
apiVersion: v1
kind: Service
metadata:
  name: web
  namespace: cloudlens-demo
spec:
  selector: {app: web}
  ports: [{port: 80}]
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: loadgen
  namespace: cloudlens-demo
spec:
  replicas: 1
  selector: {matchLabels: {app: loadgen}}
  template:
    metadata:
      labels: {app: loadgen}
    spec:
      containers:
        - name: loadgen
          image: public.ecr.aws/docker/library/busybox:stable
          command: ["/bin/sh","-c","while true; do wget -q -O /dev/null http://web.cloudlens-demo.svc.cluster.local/; sleep 1; done"]
APP
  "${K[@]}" -n cloudlens-demo rollout status deploy/web --timeout=180s || warn "web pods slow to start"
  "${K[@]}" -n cloudlens-demo rollout status deploy/loadgen --timeout=180s || warn "loadgen slow to start"
  ok "Sample app running: loadgen fetches web every second (continuous pod-to-pod HTTP)."
fi

# =====================================================================
# 6. What was built, and how to verify it end to end.
# =====================================================================
step "EKS tapping deployed"
echo "  Cluster:        ${CLUSTER} (${nodes} nodes)"
echo "  Mode:           ${MODE}"
echo "  Sensor image:   ${SENSOR_IMAGE}"
echo "  Registers to:   ${CLMS_IP} (project key ${PROJECT_KEY:0:8}...)"
echo
echo "  Verify, in order:"
echo "    kubectl --kubeconfig ${KCFG} -n ${NAMESPACE} get pods -o wide"
echo "    Then the vController UI (https://<clms>/cloudlens/login): the K8s"
echo "    sensors appear in the project within about a minute, one per"
[[ "$MODE" == "daemonset" ]] \
  && echo "    NODE, each publishing that node's pod labels for tap groups." \
  || echo "    tapped POD (sidecars register per pod)."
echo "    Pod labels become selectable in tap groups; tapped traffic follows"
echo "    the SAME tool path the VM sensors use (the vPB, when deployed)."
exit 0
