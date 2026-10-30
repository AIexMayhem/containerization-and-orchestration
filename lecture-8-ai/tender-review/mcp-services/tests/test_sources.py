import json
import shutil
from pathlib import Path

import pytest

from tender_mcp.sources import DocumentSource, ReferenceSource, SourceError


def test_package_inventory_preserves_required_documents_and_versions(data):
    result = DocumentSource(data / "tenders").list_tender_documents("demo-tender", "v1")
    assert result == {
        "tender_id": "demo-tender",
        "package_version": "v1",
        "documents": [
            {"document_id": "technical-spec", "document_version": "v1", "required": True},
            {"document_id": "draft-contract", "document_version": "v1", "required": True},
        ],
    }


def test_document_has_source_identity_text_and_addressable_sections(data):
    result = DocumentSource(data / "tenders").read_tender_document("technical-spec", "v1")
    assert result["tender_id"] == "demo-tender"
    assert result["document_version"] == "v1"
    assert "24×7" in result["text"]
    section = next(s for s in result["sections"] if s["section_id"] == "TS-2")
    assert section["source_ref"] == "technical-spec/v1#TS-2"
    assert "24×7" in section["text"]


def test_unavailable_document_is_an_error_not_empty_content(data):
    (data / "tenders/demo-tender/v1/draft-contract.md").unlink()
    with pytest.raises(SourceError) as error:
        DocumentSource(data / "tenders").read_tender_document("draft-contract", "v1")
    assert error.value.code == "DOCUMENT_UNAVAILABLE"


def test_unknown_document_version_does_not_fall_back_to_v1(data):
    with pytest.raises(SourceError) as error:
        DocumentSource(data / "tenders").read_tender_document("technical-spec", "v2")
    assert error.value.code == "UNKNOWN_DOCUMENT"


def test_unknown_package_does_not_list_another_package(data):
    with pytest.raises(SourceError) as error:
        DocumentSource(data / "tenders").list_tender_documents("other-tender", "v1")
    assert error.value.code == "PACKAGE_UNAVAILABLE"


@pytest.mark.parametrize("escape", ["../../../README.md", "linked.md"])
def test_manifest_cannot_read_outside_its_package(data, escape):
    package = data / "tenders/demo-tender/v1"
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["documents"][0]["path"] = escape
    manifest_path.write_text(json.dumps(manifest))
    if escape == "linked.md":
        (package / escape).symlink_to(data / "README.md")
    with pytest.raises(SourceError) as error:
        DocumentSource(data / "tenders").read_tender_document("technical-spec", "v1")
    assert error.value.code == "INVALID_MANIFEST"


def test_duplicate_document_identity_is_rejected_instead_of_guessing(data):
    package = data / "tenders/demo-tender/v1"
    other = Path(shutil.copytree(package, data / "tenders/other-tender/v1"))
    path = other / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["tender_id"] = "other-tender"
    path.write_text(json.dumps(manifest))
    with pytest.raises(SourceError) as error:
        DocumentSource(data / "tenders").read_tender_document("technical-spec", "v1")
    assert error.value.code == "AMBIGUOUS_DOCUMENT"


def test_reference_returns_company_capabilities_and_policy_as_separate_sources(data):
    source = ReferenceSource(data / "reference")
    profile = source.get_company_profile("demo-integrator", "v1")
    policy = source.get_evaluation_policy("tender-precheck", "v1")
    assert profile["company_id"] == "demo-integrator"
    assert profile["support_schedule"]["outside_schedule_available"] is False
    assert policy["all_package_documents_required"] is True
    assert {c["criterion_id"] for c in policy["criteria"]} == {"support_schedule", "response_time"}


def test_reference_version_is_exact_and_identifier_cannot_be_a_path(data):
    source = ReferenceSource(data / "reference")
    with pytest.raises(SourceError) as unavailable:
        source.get_company_profile("demo-integrator", "v2")
    assert unavailable.value.code == "REFERENCE_UNAVAILABLE"
    with pytest.raises(SourceError) as invalid:
        source.get_company_profile("../../README", "v1")
    assert invalid.value.code == "INVALID_IDENTIFIER"
