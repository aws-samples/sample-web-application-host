# Reserved Mode — Implementation Notes (real-deploy learnings)

Hard-won operational knowledge from actually deploying the production link to a
live account (918380168589 / us-east-1). These are things the design spec did NOT
capture but that will bite anyone who deploys this. Keep alongside the code.

## Toolchain requirements

- **CloudFront VPC Origins requires aws-cdk-lib ≥ 2.170** (L2 `VpcOrigin` API).
  2.100 has neither the L2 nor the L1 (`CfnVpcOrigin`). We run 2.261.
- **CDK CLI ≥ 2.1129** — cloud-assembly schema v54. Older CLIs (2.1108) fail with
  "schema version mismatch". Use `npx -y aws-cdk@2.1129.0`.
- Graviton (arm64) images: build with `docker buildx --platform linux/arm64`.

## Deploy gotchas (each cost a failed deploy)

1. **ASG stays at 0 instances → nothing places.**
   CDK's `AsgCapacityProvider` defaults `enableManagedScaling=True`, which zeroes
   the ASG and only scales on a capacity-provider *strategy* signal. With no
   strategy bound the cluster sits at 0. Fix: `enable_managed_scaling=False` for a
   fixed-size cluster (we manage capacity via ASG desired_capacity).

2. **ECS deployment circuit breaker + CloudFront VPC Origin = unrecoverable rollback.**
   If the breaker (or any failure) triggers a stack rollback while a VPC Origin is
   mid-create, the VPC Origin gets stuck "Deploying" — neither associable nor
   deletable → ROLLBACK_FAILED needing manual `delete-vpc-origin` surgery. We
   removed the breaker and isolated the edge (below).

3. **CloudFront edge is a SEPARATE stack (ReservedEdgeStack).**
   Root fix for #2: core stack (VPC/cluster/ASG/NLB/Envoy) is separate from the
   edge (CloudFront + VPC Origin). `edge.add_dependency(core)`. Core failures can
   no longer strand a VPC Origin; the VPC Origin only associates with an
   already-stable NLB. Still one app, deploy `--all`.

4. **EC2 API throttling (RequestLimitExceeded) can fail the LaunchTemplate.**
   Hammering describe-instances/describe-vpcs/deploy in a tight loop trips
   account-level EC2 API throttling, which can fail LaunchTemplate creation and
   cascade. Poll sparingly; back off.

5. **VPC quota.** This account is near the 5-VPC/region limit. Each dedicated-VPC
   deploy consumes one; failed deploys must fully delete before redeploying or the
   next VPC creation hits the wall. (We freed one by deleting an idle e2b VPC.)

6. **CloudFront VPC Origin create/delete is SLOW** (10-20 min to propagate). Budget
   for it; a stuck one blocks stack deletion until it reaches "Deployed" and can be
   deleted.

## Envoy config gotchas (each cost a task restart)

7. **`node.id`/`node.cluster` are required** when using any dynamic (xDS) config
   source, including filesystem `path_config_source`. Without them Envoy exits:
   "node 'id' and 'cluster' are required".

8. **Shared volume hides image-baked files.** The route-sync↔envoy shared volume
   masks cds.yaml/rds.yaml baked into the Envoy image. route-sync must SEED those
   files at runtime, and Envoy must wait for them (wait-and-run entrypoint +
   `dependsOn: START`), or Envoy exits: "path ... does not exist".

9. **ALB/NLB → Envoy needs an SG ingress rule on the instances.** In the two-stack
   / single-stack CDK this is wired via `asg.connections.allow_from(nlb_sg, ...)`.
   (Earlier public-ALB test needed a manual rule; the CDK version does it natively.)

## Density (subsystem C) — validated

- `cpu=0` at container level (NOT 64) so CPU is not a placement reservation;
  density is memory-bound. Verified ~400 idle tasks/m6g.4xlarge with
  memoryReservation=128.
- bridge high density: disable Docker userland-proxy (`{"userland-proxy": false}`)
  or dynamic host-port collisions ("address already in use") appear ~200 tasks.

## Multi-tenant routing — validated (earlier public-ALB harness)

- MT-1 (routing correctness, 10 tenants), MT-2 (1000 concurrent, 0 cross-tenant
  leakage), MT-4 (unregistered Host → 404) all PASSED end-to-end through
  ALB → Envoy → tenant. The production link re-runs these through CloudFront→NLB.
