output "policy_json" {
  description = "The developer policy document, for teams that manage the role elsewhere."
  value       = data.aws_iam_policy_document.this.json
}
