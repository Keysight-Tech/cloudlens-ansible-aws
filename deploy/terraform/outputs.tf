# =====================================================================
# CloudLens Stack (AWS) - Terraform outputs
# =====================================================================
# deploy-stack.sh reads these with `terraform output -raw <name>`. Keep the
# output names in sync with the tf_output() calls in deploy-stack.sh:
#   controller_public_ip, kvo_public_ip, vpb_public_ip.

output "vpc_id" {
  description = "VPC created for the stack."
  value       = aws_vpc.this.id
}

output "controller_public_ip" {
  description = "vController Elastic IP."
  value       = aws_eip.controller.public_ip
}

output "kvo_public_ip" {
  description = "KVO Elastic IP (empty when KVO is not deployed)."
  value       = length(aws_eip.kvo) > 0 ? aws_eip.kvo[0].public_ip : ""
}

output "vpb_public_ip" {
  description = "vPB management Elastic IP (empty when vPB is not deployed)."
  value       = length(aws_eip.vpb) > 0 ? aws_eip.vpb[0].public_ip : ""
}
