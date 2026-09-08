# SPDX-License-Identifier: Apache-2.0
"""Deployment-only configuration for real Core/Object path tests."""

from meridian_storage import Meridian, ResourceRef, RuntimeConfig
from meridian_storage.adapters.oci import OciDistributionBinding, configured_capability_manifest
from meridian_storage.object_common import ObjectCatalogProvider
from meridian_storage.registry import NamespaceDefinition, ResourceBundle, ResourceDefinition
from meridian_storage.runtime.config import BindingConfig, ClientPolicy, SecretReference, TLSPolicy
from meridian_storage.spi import AdapterCreateContext, SecretValue

RESOURCE = ResourceRef("object", "resources", "objects")


def binding_config(
    endpoint="https://registry.test",
    repository="meridian/test",
    *,
    registry_release=None,
    registry_image=None,
):
    config = OciDistributionBinding(
        resource=RESOURCE,
        endpoint=endpoint,
        repository=repository,
        allow_insecure_http=endpoint.startswith("http:"),
        deletion_enabled=True,
        registry_release=registry_release,
        registry_image=registry_image,
    )
    return BindingConfig(
        id="objects",
        adapter_id="oci-distribution",
        adapter_contract="1.0.0",
        engine_profile="oci-distribution",
        engine_version="1.1.1",
        endpoint=endpoint,
        service_ref=None,
        physical_namespace=repository,
        tls=TLSPolicy(
            "disabled" if endpoint.startswith("http:") else "server",
            None if endpoint.startswith("http:") else "registry.test",
            None if endpoint.startswith("http:") else SecretReference("test", "ca"),
            None,
        ),
        identity_ref=SecretReference("test", "identity"),
        secret_ref=SecretReference("test", "credential"),
        client=ClientPolicy(1, 4, 1000, 10000, 30000, 1000000, 30000),
        required_capability_fingerprint=configured_capability_manifest(config).fingerprint,
        required_physical_fingerprint=None,
        compatibility_pins={"adapterContract": "1.0.0", "engineVersion": "1.1.1"},
        settings={
            "resource": RESOURCE.canonical,
            "authMode": "anonymous",
            "deletionEnabled": True,
            **({"registryRelease": registry_release} if registry_release is not None else {}),
            **({"registryImage": registry_image} if registry_image is not None else {}),
        },
        extensions={},
    )


def create_context(binding=None):
    return AdapterCreateContext(
        binding or binding_config(),
        SecretValue(b"test-user"),
        SecretValue(b"test-password"),
        SecretValue(b"test-ca") if (binding or binding_config()).tls.mode == "server" else None,
    )


def core_runtime(binding, *, identity=b"unused-anonymous", credential=b"unused-anonymous"):
    resource = ResourceDefinition(RESOURCE, "object")
    bundle = ResourceBundle(
        "oci.test.schemas",
        "1.0.0",
        "1.0.0",
        namespaces=(NamespaceDefinition("object", "resources"),),
        resources=(resource,),
    )

    class Schemas:
        provider_id = bundle.provider_id
        provider_contract_version = "1.0.0"

        def load(self):
            return bundle

    class Secrets:
        def resolve(self, reference):
            return SecretValue(identity if reference.reference == "identity" else credential)

    provider = ObjectCatalogProvider()
    config = RuntimeConfig.from_mapping(
        {
            "formatVersion": "meridian-config.v1",
            "profile": "conformance",
            "catalogs": {
                "providers": [
                    {
                        "name": "object",
                        "package": "meridian-storage-object-common",
                        "contract": provider.manifest().catalog_contract_version,
                        "requiredFingerprint": provider.manifest().fingerprint,
                    }
                ],
                "extensions": {},
            },
            "resources": {
                "pins": [
                    {
                        "ref": RESOURCE.to_dict(),
                        "providerId": bundle.provider_id,
                        "requiredFingerprint": resource.fingerprint,
                    }
                ],
                "extensions": {},
            },
            "schemas": {
                "providers": [
                    {
                        "id": bundle.provider_id,
                        "package": "oci-test-schemas",
                        "contract": "1.0.0",
                        "requiredFingerprint": bundle.fingerprint,
                    }
                ],
                "live": {"enabled": False, "required": False, "providerId": None},
                "extensions": {},
            },
            "bindings": [binding.to_dict()],
            "placements": [
                {
                    "id": "objects",
                    "selector": {"resources": [RESOURCE.to_dict()], "catalog": None, "labels": {}},
                    "bindingId": binding.id,
                    "extensions": {},
                }
            ],
            "validation": {
                "strict": True,
                "requirePhysicalFingerprints": False,
                "defaultOperationTimeoutMs": 10000,
                "idempotencyCacheEntries": 64,
                "retry": {"maxAttempts": 1, "baseDelayMs": 0, "maxDelayMs": 0, "jitterRatio": 0},
            },
        }
    )
    # Only test-owned schema and secret providers are injected. Catalog and
    # adapter discovery use normal installed entry points, including siblings.
    return Meridian(config, schema_providers=(Schemas(),), secret_resolver=Secrets())
