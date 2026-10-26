terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

variable "versions" {
  description = "Releases to run, one instance each, e.g. [\"0.4.0\"]. To update, add the new release and remove the old one once Bullet trusts the new address: removing a release terminates its instance and destroys its key."
  type        = set(string)
}

variable "bucket" {
  description = "Signature bucket, in the instances' region. Changing it replaces the instances and their keys."
  type        = string
}

variable "prefix" {
  description = "Key prefix in the bucket; each key signs under <prefix>/<address>. Changing it replaces the instances and their keys."
  type        = string
}

variable "subnet_id" {
  description = "Subnet with outbound internet access (a public IP or NAT)."
  type        = string
}

variable "instance_type" {
  description = "A Graviton (arm64) type that supports NitroTPM attestation documents, which T4g does not."
  type        = string
  default     = "c7g.medium"
}

variable "name" {
  description = "Name of the created resources."
  type        = string
  default     = "hyperlane-bullet-validator"
}

data "aws_subnet" "this" {
  id = var.subnet_id
}

data "aws_ami" "this" {
  for_each = var.versions

  owners = ["010222938823"] # Bullet
  filter {
    name   = "name"
    values = ["hyperlane-bullet-validator-${each.key}"]
  }
  # Public AMIs are deprecated after two years but stay launchable.
  include_deprecated = true
}

# No ingress rules: nothing on the instance accepts connections.
resource "aws_security_group" "this" {
  name_prefix = "${var.name}-"
  vpc_id      = data.aws_subnet.this.vpc_id

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_iam_role" "this" {
  name_prefix = "${var.name}-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "this" {
  role = aws_iam_role.this.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "arn:aws:s3:::${var.bucket}/${var.prefix}/*"
    }]
  })
}

resource "aws_iam_instance_profile" "this" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.this.name
}

resource "aws_instance" "this" {
  for_each = var.versions

  ami                    = data.aws_ami.this[each.key].id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.this.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name
  user_data              = "s3://${var.bucket}/${var.prefix}"
  # A changed location needs a fresh key attested there, not a restart.
  user_data_replace_on_change = true

  metadata_options {
    http_tokens = "required"
  }

  tags = { Name = "${var.name}-${each.key}" }
}

output "instance_ids" {
  description = "Instance ID per release, e.g. for `aws ec2 get-console-output`."
  value       = { for version, instance in aws_instance.this : version => instance.id }
}

output "role_arn" {
  description = "Role the instances write as. A bucket in another account must grant it s3:PutObject on the prefix."
  value       = aws_iam_role.this.arn
}
