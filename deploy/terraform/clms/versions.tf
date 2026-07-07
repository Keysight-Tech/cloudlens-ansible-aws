terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 6.0"
    }
  }
}

# NOTE: This module deliberately omits a provider "aws" block so it can be
# wrapped by deploy/terraform/stack/ with count or for_each. For standalone use,
# add a provider.tf in your working directory or copy provider.tf.example.
