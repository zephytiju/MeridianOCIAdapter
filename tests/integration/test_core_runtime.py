# SPDX-License-Identifier: Apache-2.0
"""Installed Core discovery and Object execution against genuine Distribution."""

import hashlib
import os
import secrets
from io import BytesIO

import pytest

from meridian_storage import OperationContext
from meridian_storage.object_common import (
    ByteRange,
    DigestMismatch,
    ImmutabilityRequest,
    ImmutableObjectConflict,
    ObjectNotFound,
    PayloadReference,
    default_payload_registry,
    transfer_payload,
)

from ..support.core import binding_config, core_runtime


@pytest.mark.integration
@pytest.mark.parametrize("selected_provenance", [False, True])
def test_normal_core_object_runtime(selected_provenance):
    endpoint = os.getenv("MERIDIAN_OCI_TEST_ENDPOINT")
    repository = os.getenv("MERIDIAN_OCI_TEST_REPOSITORY")
    if not endpoint or not repository:
        pytest.skip("a disposable OCI registry endpoint and repository are required")
    binding = binding_config(
        endpoint,
        repository + "/core-" + secrets.token_hex(8),
        registry_release=os.getenv("MERIDIAN_OCI_TEST_RELEASE") if selected_provenance else None,
        registry_image=os.getenv("MERIDIAN_OCI_TEST_IMAGE") if selected_provenance else None,
    )
    runtime = core_runtime(binding)
    payloads = default_payload_registry()
    content = b"resource-store-compatible-object-stream"
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    reference = None
    tokens = []
    try:
        runtime.start()
        objects = runtime.catalog("object")
        with runtime.context(OperationContext("test:oci")):
            payload = payloads.register_stream(
                BytesIO(content), expected_length=len(content), expected_digest=digest
            )
            tokens.append(payload.token)
            put = objects.put(
                resource="resources.objects",
                object_id="artifact/1",
                payload=payload,
                media_type="application/octet-stream",
                expected_length=len(content),
                expected_digest=digest,
                create_only=True,
                immutability=ImmutabilityRequest("immutable", True),
            )
            result = runtime.execute(put).data
            reference = result["metadata"]["objectRef"]
            assert result["metadata"]["digest"] == digest
            for duplicate in (content, b"conflicting-content"):
                duplicate_payload = payloads.register_stream(BytesIO(duplicate))
                tokens.append(duplicate_payload.token)
                with pytest.raises(ImmutableObjectConflict):
                    runtime.execute(
                        objects.put(
                            resource="resources.objects",
                            object_id="artifact/1",
                            payload=duplicate_payload,
                            media_type="application/octet-stream",
                            create_only=True,
                            immutability=ImmutabilityRequest("immutable", True),
                        )
                    )
            assert (
                runtime.execute(
                    objects.stat(resource="resources.objects", reference=reference)
                ).data["metadata"]["digest"]
                == digest
            )
            received = runtime.execute(
                objects.get(resource="resources.objects", reference=reference)
            ).data
            received_payload = PayloadReference.from_mapping(received["payload"])
            tokens.append(received_payload.token)
            sink = BytesIO()
            transfer_payload(received_payload, payloads, sink)
            assert sink.getvalue() == content
            selected = runtime.execute(
                objects.read_range(
                    resource="resources.objects", reference=reference, byte_range=ByteRange(2, 8)
                )
            ).data
            selected_payload = PayloadReference.from_mapping(selected["payload"])
            tokens.append(selected_payload.token)
            sink = BytesIO()
            transfer_payload(selected_payload, payloads, sink)
            assert sink.getvalue() == content[2:9]
            bad = payloads.register_stream(
                BytesIO(b"corrupt"), expected_digest="sha256:" + "0" * 64
            )
            tokens.append(bad.token)
            with pytest.raises(DigestMismatch):
                runtime.execute(
                    objects.put(
                        resource="resources.objects",
                        object_id="invalid",
                        payload=bad,
                        media_type="application/octet-stream",
                    )
                )
            runtime.execute(objects.delete(resource="resources.objects", reference=reference))
            with pytest.raises(ObjectNotFound):
                runtime.execute(objects.stat(resource="resources.objects", reference=reference))
            reference = None
    finally:
        if reference is not None:
            with runtime.context(OperationContext("test:cleanup")):
                runtime.execute(
                    runtime.catalog("object").delete(
                        resource="resources.objects", reference=reference
                    )
                )
        runtime.close()
        for token in tokens:
            payloads.release(token)


@pytest.mark.integration
def test_s3_startup_with_oci_installed():
    from dataclasses import replace

    endpoint = os.getenv("MERIDIAN_S3_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("a disposable S3 endpoint is required")
    import boto3

    from meridian_storage.adapters.s3 import S3Config, s3_capability_manifest

    # These are public, disposable CI fixture values, not deployment credentials.
    identity, credential = b"meridian-test", b"meridian-test-password"
    bucket = "oci-coexist-" + secrets.token_hex(8)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=identity.decode(),
        aws_secret_access_key=credential.decode(),
    )
    client.create_bucket(Bucket=bucket)
    config = S3Config(
        bucket=bucket, prefix="objects", endpoint_url=endpoint, allow_insecure_http=True
    )
    binding = replace(
        binding_config(endpoint, bucket + "/objects"),
        adapter_id="s3",
        engine_profile=config.engine_profile,
        engine_version=config.engine_version,
        settings={"allowInsecureHttp": True},
        compatibility_pins={"adapterContract": "1.0.0", "engineVersion": config.engine_version},
        required_capability_fingerprint=s3_capability_manifest(config).fingerprint,
    )
    runtime = core_runtime(binding, identity=identity, credential=credential)
    try:
        runtime.start()
        assert runtime.catalog("object") is not None
    finally:
        runtime.close()
        client.delete_bucket(Bucket=bucket)
        client.close()
