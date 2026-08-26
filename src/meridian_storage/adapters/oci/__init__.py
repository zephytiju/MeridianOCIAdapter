# SPDX-License-Identifier: Apache-2.0
"""OCI Distribution 1.1 Adapter for the Meridian V1 Object Catalog."""

from ._version import __version__
from .adapter import OciDistributionAdapter
from .descriptor import (
    ADAPTER_CONTRACT_VERSION,
    ADAPTER_ID,
    ENGINE_PROFILE,
    OCI_DISTRIBUTION_VERSION,
    AnonymousCredentials,
    BasicCredentials,
    BearerCredentials,
    CallbackCredentials,
    CredentialProvider,
    OciDistributionBinding,
    configured_capability_manifest,
    oci_distribution_descriptor,
)
from .documents import compatibility_document
from .migration import LogicalObjectExport, OciLogicalMigration
from .probe import OciDistributionProbe, OciProbeReport
from .transport import RegistryHttpClient

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "ADAPTER_ID",
    "ENGINE_PROFILE",
    "OCI_DISTRIBUTION_VERSION",
    "AnonymousCredentials",
    "BasicCredentials",
    "BearerCredentials",
    "CallbackCredentials",
    "CredentialProvider",
    "LogicalObjectExport",
    "OciDistributionAdapter",
    "OciDistributionBinding",
    "OciDistributionProbe",
    "OciLogicalMigration",
    "OciProbeReport",
    "RegistryHttpClient",
    "__version__",
    "compatibility_document",
    "configured_capability_manifest",
    "oci_distribution_descriptor",
]
