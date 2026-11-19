import base64
import hashlib
import io
import unittest
from unittest.mock import patch

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber

from tesla_invoice.archive import Archive


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.pdf = b"%PDF-1.4\n%%EOF\n"
        self.digest = hashlib.sha256(self.pdf).hexdigest()
        self.key = f"tesla-invoices/supercharger/{self.digest}.pdf"
        self.client = boto3.client(
            "s3", region_name="eu-central-1", aws_access_key_id="test", aws_secret_access_key="test"
        )
        with patch.dict("os.environ", {"S3_BUCKET": "shared-bucket"}), patch(
            "boto3.client", return_value=self.client
        ):
            self.archive = Archive()
        self.stub = self.enterContext(Stubber(self.client))
        self.put = {
            "Bucket": "shared-bucket",
            "Key": self.key,
            "Body": self.pdf,
            "ContentType": "application/pdf",
            "ServerSideEncryption": "AES256",
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(self.digest)).decode("ascii"),
            "IfNoneMatch": "*",
        }

    def test_conditional_put_uses_scoped_key_and_checksum(self):
        self.stub.add_response("put_object", {}, self.put)
        self.assertEqual(self.archive.save("supercharger", self.digest, self.pdf), self.key)
        self.stub.assert_no_pending_responses()

    def test_confirm_existing_object_after_retry(self):
        for data in (self.pdf, b"unexpected bytes"):
            with self.subTest(data=data):
                self.stub.add_client_error(
                    "put_object", service_error_code="PreconditionFailed", http_status_code=412,
                    expected_params=self.put,
                )
                self.stub.add_response(
                    "get_object", {"Body": StreamingBody(io.BytesIO(data), len(data))},
                    {"Bucket": "shared-bucket", "Key": self.key},
                )
                if data == self.pdf:
                    self.assertEqual(self.archive.save("supercharger", self.digest, self.pdf), self.key)
                else:
                    with self.assertRaisesRegex(ValueError, "differs"):
                        self.archive.save("supercharger", self.digest, self.pdf)
                self.stub.assert_no_pending_responses()
