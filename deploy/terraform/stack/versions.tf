terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 6.0"
    }
  }
}

# The stack is the root module for a full-stack deploy, so it configures the
# provider here. Child modules (../clms, ../kvo, ../vpb) inherit this provider.
provider "aws" {
  region  = var.region
  profile = var.profile != "" ? var.profile : null
}
