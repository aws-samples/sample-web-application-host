"""
ReservedEdgeStack — CloudFront edge, isolated from the core stack.

Contains ONLY: CloudFront Distribution + VPC Origin (→ core NLB) + Origin Request
Policy (forwards Host, replaces Lambda@Edge per ADR-2). Spec §2.1 ingress.

Why a separate stack (learned the hard way): a CloudFront VPC Origin cancelled
mid-create — by ANY failure elsewhere in a big stack (EC2 API throttling, slow
instance, circuit breaker) — becomes stuck in a "Deploying" limbo that is neither
associable nor deletable, forcing ROLLBACK_FAILED manual surgery. Keeping the edge
in its own small stack, deployed AFTER the core NLB is stable, means:
  * the VPC Origin only ever associates with an already-Deployed NLB,
  * core failures can't strand a VPC Origin,
  * if the edge itself hiccups, only this tiny stack rolls back.
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_ec2 as ec2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_elasticloadbalancingv2 as elbv2,
)
from constructs import Construct


class ReservedEdgeStack(Stack):
    """CloudFront (VPC Origin → NLB) + Host-forwarding Origin Request Policy."""

    def __init__(self, scope: Construct, construct_id: str, config, nlb, nlb_sg, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        for k, v in config.get_tags().items():
            Tags.of(self).add(k, v)

        # CRITICAL: allow CloudFront VPC Origin traffic into the NLB on :80.
        # CDK's VpcOrigin construct does NOT open the NLB SG automatically, so
        # without this the NLB SG has no inbound rule and CloudFront origin
        # requests time out (HTTP 000). Source = the CloudFront origin-facing
        # managed prefix list (stable, referenceable), which covers the
        # service-managed CloudFront-VPCOrigins ENIs.
        cf_prefix_list = config.get(
            "CloudFront", "origin_facing_prefix_list", "APP_CF_ORIGIN_PREFIX_LIST",
            fallback="pl-3b927c52")  # com.amazonaws.global.cloudfront.origin-facing (us-east-1)
        nlb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cf_prefix_list),
            ec2.Port.tcp(80),
            "CloudFront VPC Origin to NLB")

        cert = acm.Certificate.from_certificate_arn(
            self, "Cert", config.get("CloudFront", "certificate_arn", "APP_CERTIFICATE_ARN"))
        domain = config.get("CloudFront", "domain_name", "APP_DOMAIN_NAME")

        # VPC Origin → internal NLB, HTTP (ADR-4 Day-1: viewer TLS ends at CloudFront;
        # the private CloudFront→NLB→Envoy hop is HTTP inside the VPC).
        vpc_origin = origins.VpcOrigin.with_network_load_balancer(
            nlb, protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY, http_port=80)

        # Forward Host so Envoy can route by tenant subdomain (ADR-2, no Lambda@Edge).
        orp = cloudfront.OriginRequestPolicy(
            self, "ForwardHost", origin_request_policy_name="ReservedForwardHost",
            header_behavior=cloudfront.OriginRequestHeaderBehavior.all(),
            cookie_behavior=cloudfront.OriginRequestCookieBehavior.all(),
            query_string_behavior=cloudfront.OriginRequestQueryStringBehavior.all())

        distribution = cloudfront.Distribution(
            self, "Cdn",
            default_behavior=cloudfront.BehaviorOptions(
                origin=vpc_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,  # dynamic tenants
                origin_request_policy=orp),
            domain_names=[domain], certificate=cert,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021)

        CfnOutput(self, "CloudFrontDomain", value=distribution.distribution_domain_name)
        CfnOutput(self, "DnsTarget",
                  value=f"CNAME {domain.lstrip('*.')} -> {distribution.distribution_domain_name}")
