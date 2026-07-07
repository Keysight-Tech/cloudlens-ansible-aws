###############################################################################
# Stack outputs: surface child module outputs as lists (one entry per instance)
###############################################################################

output "vpc_id" {
  description = "ID of the shared VPC (null when shared_vpc = false)."
  value       = var.shared_vpc ? aws_vpc.shared[0].id : null
}

output "region" {
  description = "AWS region the stack is deployed in."
  value       = var.region
}

###############################################################################
# vController (formerly CLMS) - lists, one per instance
###############################################################################

output "vcontroller_public_ips" {
  description = "Public IPs of every vController instance (length = vcontroller_count)."
  value       = module.clms[*].clms_public_ip
}

output "vcontroller_private_ips" {
  description = "Private IPs of every vController instance."
  value       = module.clms[*].clms_private_ip
}

output "vcontroller_ui_urls" {
  description = "HTTPS URLs for every vController web console."
  value       = module.clms[*].clms_ui_url
}

output "vcontroller_ssh_commands" {
  description = "SSH commands for OS-level access to every vController instance."
  value       = module.clms[*].ssh_command
}

output "vcontroller_default_credentials" {
  description = "Default vController web console credentials. Change immediately on first login."
  value       = length(module.clms) > 0 ? module.clms[0].default_credentials : null
}

# Backward-compatible single-instance aliases (first instance only)
output "clms_public_ip" {
  description = "Public IP of the first vController instance (kept for back-compat; prefer vcontroller_public_ips)."
  value       = length(module.clms) > 0 ? module.clms[0].clms_public_ip : null
}

output "clms_ui_url" {
  description = "HTTPS URL of the first vController web console (kept for back-compat)."
  value       = length(module.clms) > 0 ? module.clms[0].clms_ui_url : null
}

###############################################################################
# KVO - lists, one per instance (length = kvo_count if deploy_kvo else 0)
###############################################################################

output "kvo_public_ips" {
  description = "Public IPs of every KVO instance. Empty list when not deployed."
  value       = module.kvo[*].kvo_public_ip
}

output "kvo_ui_urls" {
  description = "HTTPS URLs for every KVO web console. Empty list when not deployed."
  value       = module.kvo[*].kvo_ui_url
}

output "kvo_ssh_commands" {
  description = "SSH commands for every KVO instance."
  value       = module.kvo[*].ssh_command
}

###############################################################################
# vPB - lists, one per instance
###############################################################################

output "vpb_public_ips" {
  description = "Public IPs of every vPB management NIC. Empty list when not deployed."
  value       = module.vpb[*].vpb_public_ip
}

output "vpb_ssh_commands" {
  description = "SSH commands (port 9022) for every vPB instance."
  value       = module.vpb[*].vpb_ssh_command
}

output "vpb_cli_access" {
  description = "How to reach the vPB CLI on each instance after auto-bootstrap."
  value       = module.vpb[*].vpb_cli_access
}

output "vpb_private_ips" {
  description = "Private IPs per vPB instance: { mgmt = string, ingress = list, egress = list }."
  value = [
    for v in module.vpb : {
      mgmt    = v.mgmt_private_ip
      ingress = v.ingress_private_ips
      egress  = v.egress_private_ips
    }
  ]
}

###############################################################################
# Hand-off
###############################################################################

output "summary" {
  description = "Human-readable summary of what was deployed."
  value = format(
    "Deployed %d vController, %d KVO, %d vPB in %s. First vController console: %s",
    length(module.clms),
    length(module.kvo),
    length(module.vpb),
    var.region,
    length(module.clms) > 0 ? module.clms[0].clms_ui_url : "n/a"
  )
}

output "next_step" {
  description = "What to do once the stack is up."
  value       = length(module.clms) > 0 ? "Open ${module.clms[0].clms_ui_url} (vController), sign in with admin / Cl0udLens@dm!n, change the password, create a project, and copy the project key. Then run quickstart.sh to deploy sensors to your tagged EC2 instances." : "No vController deployed (vcontroller_count = 0). Stack has no entry point."
}
