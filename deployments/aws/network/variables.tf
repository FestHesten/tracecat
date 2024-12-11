## AWS provider variables

variable "aws_region" {
  type        = string
  description = "AWS region (secrets and hosted zone must be in the same region)"
}

## DNS

variable "domain_name" {
  type        = string
  description = "The domain name to use for Tracecat"
}

variable "hosted_zone_id" {
  type        = string
  description = "The hosted zone ID associated with the Tracecat domain"
}

variable "temporal_ui_domain_name" {
  type        = string
  description = "The domain name to use for the Temporal UI"
}

variable "temporal_ui_hosted_zone_id" {
  type        = string
  description = "The hosted zone ID associated with the Temporal UI domain"
}
