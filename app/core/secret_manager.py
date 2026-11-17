def access_secret_version(secret_id: str, project_number: str, version_id: str = "latest") -> str:
    """Read a secret from GCP Secret Manager (prod only)."""
    if not secret_id or not project_number:
        raise ValueError("secret id and PROJECT_NUMBER are required to read a secret")

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_number}/secrets/{secret_id}/versions/{version_id}"
    return client.access_secret_version(name=name).payload.data.decode("UTF-8")
