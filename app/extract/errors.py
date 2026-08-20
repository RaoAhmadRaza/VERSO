"""Typed extraction failures.

Every rejection carries a sentence written for the person who uploaded the file. PLAN.md
§6.3 is emphatic that silent failure is the enemy: naming the actual problem ("this PDF has
no text layer -- it's a scan") is the whole point.
"""


class ExtractionError(Exception):
    """Base class. `message` is user-facing; `code` is for the UI and tests."""

    code = "extraction_failed"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class UnsupportedFormatError(ExtractionError):
    code = "unsupported_format"


class PasswordProtectedError(ExtractionError):
    code = "password_protected"


class CorruptFileError(ExtractionError):
    code = "corrupt_file"


class FileTooLargeError(ExtractionError):
    code = "file_too_large"


class EmptyDocumentError(ExtractionError):
    code = "empty_document"


class ScannedDocumentError(ExtractionError):
    code = "scanned_no_text_layer"


class GarbledTextError(ExtractionError):
    code = "garbled_text"
