output "vpb_public_ip" {
  description = "Public (Elastic) IP address of the vPB management NIC."
  value       = aws_eip.vpb_mgmt.public_ip
}

output "vpb_ssh_command" {
  description = "SSH command for OS-level access (port 9022, KCOS layout)."
  value       = "ssh -p 9022 ${var.admin_username}@${aws_eip.vpb_mgmt.public_ip}"
}

output "vpb_cli_access" {
  description = "vPB CLI access after auto-bootstrap completes."
  value       = "After deploy completes, SSH in and run: sudo vpb. user_data auto-installed kubeconfig + the /usr/local/bin/vpb wrapper. Bootstrap log: /var/log/cloudlens-bootstrap.log on the instance."
}

output "next_step" {
  description = "What to do once vPB is up."
  value       = "vPB is ready. Bootstrap ran automatically via user_data during deploy. SSH in: ssh -p 9022 ${var.admin_username}@${aws_eip.vpb_mgmt.public_ip}. Then: sudo kubectl get pods -A and sudo vpb. Configure ingress filters via sudo vpb. See docs/OPERATIONS.md."
}

output "mgmt_private_ip" {
  description = "Private IP of the management NIC."
  value       = aws_network_interface.mgmt.private_ip
}

output "ingress_private_ips" {
  description = "Private IPs of every ingress NIC (one per count.index)."
  value       = [for nic in aws_network_interface.ingress : nic.private_ip]
}

output "egress_private_ips" {
  description = "Private IPs of every egress NIC (one per count.index)."
  value       = [for nic in aws_network_interface.egress : nic.private_ip]
}

output "ingress_nic_ids" {
  description = "ENI IDs of every ingress NIC."
  value       = [for nic in aws_network_interface.ingress : nic.id]
}

output "egress_nic_ids" {
  description = "ENI IDs of every egress NIC."
  value       = [for nic in aws_network_interface.egress : nic.id]
}

output "instance_id" {
  description = "EC2 instance ID of the vPB."
  value       = aws_instance.vpb.id
}
