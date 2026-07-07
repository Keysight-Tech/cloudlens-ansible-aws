output "kvo_public_ip" {
  description = "Public (Elastic) IP address of the KVO instance."
  value       = aws_eip.kvo.public_ip
}

output "kvo_private_ip" {
  description = "Private IP address of the KVO instance."
  value       = aws_instance.kvo.private_ip
}

output "kvo_ui_url" {
  description = "HTTPS URL for the KVO web console."
  value       = "https://${aws_eip.kvo.public_ip}"
}

output "default_credentials" {
  description = "Default KVO web console credentials. Refer to Keysight KVO documentation."
  value       = "See Keysight KVO documentation"
}

output "next_step" {
  description = "What to do once KVO is up."
  value       = "After KVO initializes (about 15 minutes), open the console and register your vController and vPB fleet for centralized orchestration."
}

output "ssh_command" {
  description = "SSH command for OS-level access to the KVO instance."
  value       = "ssh ${var.admin_username}@${aws_eip.kvo.public_ip}"
}

output "instance_id" {
  description = "EC2 instance ID of the KVO."
  value       = aws_instance.kvo.id
}
