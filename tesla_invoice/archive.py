import base64
import os

PREFIX = "tesla-invoices/"


class Archive:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = os.environ["S3_BUCKET"].strip()
        if not self.bucket:
            raise ValueError("S3_BUCKET is required")
        self.client = boto3.client(
            "s3",
            region_name=os.getenv("AWS_DEFAULT_REGION") or None,
            config=Config(connect_timeout=10, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}),
        )

    def save(self, category, digest, pdf):
        from botocore.exceptions import ClientError

        key = f"{PREFIX}{category}/{digest}.pdf"
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=pdf,
                ContentType="application/pdf",
                ServerSideEncryption="AES256",
                ChecksumSHA256=checksum,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            existing = self.client.get_object(Bucket=self.bucket, Key=key)
            with existing["Body"] as body:
                if body.read(len(pdf) + 1) != pdf:
                    raise ValueError("Existing S3 object differs from the invoice") from exc
        return key
