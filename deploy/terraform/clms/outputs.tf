output "clms_public_ip" {
  description = "Public (Elastic) IP address of the vController instance."
  value       = aws_eip.clms.public_ip
}

output "clms_private_ip" {
  description = "Private IP address of the vController instance."
  value       = aws_instance.clms.private_ip
}

output "clms_ui_url" {
  description = "HTTPS URL for the vController web console."
  value       = "https://${aws_eip.clms.public_ip}"
}

output "default_credentials" {
  description = "Default vController web console credentials. Change immediately after first login."
  value       = "admin / Cl0udLens@dm!n (change on first login)"
}

output "next_step" {
  description = "What to do once the vController is up."
  value       = "After the vController initializes (about 15 minutes), open the console, create a project, and copy the project key. Then return to the Ansible site to deploy sensors."
}

output "ssh_command" {
  description = "SSH command for OS-level access to the vController instance."
  value       = "ssh ${var.admin_username}@${aws_eip.clms.public_ip}"
}

output "instance_id" {
  description = "EC2 instance ID of the vController."
  value       = aws_instance.clms.id
}
