"""Read-only access to versioned tender documents and company references."""

import json
import re
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SECTION = re.compile(r"^##\s+([A-Z]+-\d+)\.\s+(.+)$", re.MULTILINE)


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise SourceError("INVALID_IDENTIFIER", "Use an identifier, not a file path")


def _inside(path: Path, root: Path, code: str) -> Path:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise SourceError(code, "Cannot resolve source path") from exc
    if not resolved.is_relative_to(root):
        raise SourceError(code, "Source path leaves its allowed directory")
    return resolved


def _read_json(path: Path, unavailable_code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceError(unavailable_code, "Requested source is unavailable") from exc
    except (ValueError, UnicodeError) as exc:
        raise SourceError("INVALID_SOURCE", "Requested source is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise SourceError("INVALID_SOURCE", "Requested source must be a JSON object")
    return value


class DocumentSource:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _manifest(self, tender_id: str, package_version: str) -> tuple[Path, dict]:
        _identifier(tender_id)
        _identifier(package_version)
        package = _inside(self.root / tender_id / package_version, self.root, "INVALID_MANIFEST")
        manifest_path = _inside(package / "manifest.json", package, "INVALID_MANIFEST")
        manifest = _read_json(manifest_path, "PACKAGE_UNAVAILABLE")
        if (
            manifest.get("tender_id") != tender_id
            or manifest.get("package_version") != package_version
            or not isinstance(manifest.get("documents"), list)
            or not manifest["documents"]
        ):
            raise SourceError("INVALID_MANIFEST", "Package metadata does not match the request")
        identities = set()
        for doc in manifest["documents"]:
            if not isinstance(doc, dict):
                raise SourceError("INVALID_MANIFEST", "Invalid document entry")
            for key in ("document_id", "document_version"):
                value = doc.get(key)
                if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                    raise SourceError("INVALID_MANIFEST", "Invalid document identity")
            identity = (doc["document_id"], doc["document_version"])
            if identity in identities:
                raise SourceError("INVALID_MANIFEST", "Duplicate document identity")
            identities.add(identity)
            if not isinstance(doc.get("required"), bool) or not isinstance(doc.get("path"), str):
                raise SourceError("INVALID_MANIFEST", "Document requires a path and required flag")
            if not doc["path"] or Path(doc["path"]).is_absolute():
                raise SourceError("INVALID_MANIFEST", "Document path must be relative")
            _inside(package / doc["path"], package, "INVALID_MANIFEST")
        return package, manifest

    def list_tender_documents(self, tender_id: str, package_version: str) -> dict:
        _, manifest = self._manifest(tender_id, package_version)
        return {
            "tender_id": tender_id,
            "package_version": package_version,
            "documents": [
                {key: doc[key] for key in ("document_id", "document_version", "required")}
                for doc in manifest["documents"]
            ],
        }

    def read_tender_document(self, document_id: str, document_version: str) -> dict:
        _identifier(document_id)
        _identifier(document_version)
        matches = []
        for path in sorted(self.root.glob("*/*/manifest.json")):
            package, manifest = self._manifest(path.parent.parent.name, path.parent.name)
            for doc in manifest["documents"]:
                if (doc["document_id"], doc["document_version"]) == (document_id, document_version):
                    matches.append((package, manifest, doc))
        if not matches:
            raise SourceError("UNKNOWN_DOCUMENT", "Requested document version is not in a package")
        if len(matches) > 1:
            raise SourceError("AMBIGUOUS_DOCUMENT", "Document identity occurs in multiple packages")
        package, manifest, doc = matches[0]
        path = _inside(package / doc["path"], package, "INVALID_MANIFEST")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SourceError("DOCUMENT_UNAVAILABLE", "Cannot read the requested document") from exc
        headings = list(SECTION.finditer(text))
        sections = []
        for index, heading in enumerate(headings):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            sections.append(
                {
                    "section_id": heading[1],
                    "title": heading[2],
                    "text": text[heading.end() : end].strip(),
                    "source_ref": f"{document_id}/{document_version}#{heading[1]}",
                }
            )
        return {
            "tender_id": manifest["tender_id"],
            "package_version": manifest["package_version"],
            "document_id": document_id,
            "document_version": document_version,
            "text": text,
            "sections": sections,
        }


class ReferenceSource:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _read(self, directory: str, identifier: str, version: str, id_key: str, version_key: str):
        _identifier(identifier)
        _identifier(version)
        path = _inside(
            self.root / directory / identifier / f"{version}.json", self.root, "INVALID_SOURCE"
        )
        value = _read_json(path, "REFERENCE_UNAVAILABLE")
        if value.get(id_key) != identifier or value.get(version_key) != version:
            raise SourceError("INVALID_SOURCE", "Reference metadata does not match the request")
        return value

    def get_company_profile(self, company_id: str, profile_version: str) -> dict:
        return self._read("companies", company_id, profile_version, "company_id", "profile_version")

    def get_evaluation_policy(self, policy_id: str, policy_version: str) -> dict:
        return self._read("policies", policy_id, policy_version, "policy_id", "policy_version")
