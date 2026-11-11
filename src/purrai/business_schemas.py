"""Transport schemas for the editable policy document."""

from pydantic import BaseModel, Field, field_validator

from purrai.business_validation import validate_document


class BusinessDocumentInput(BaseModel):
    document: dict

    @field_validator("document")
    @classmethod
    def valid_document(cls, value):
        return validate_document(value)


class BusinessPolicyInput(BusinessDocumentInput):
    request_id: str = Field(min_length=8, max_length=80)


class BusinessPreviewInput(BusinessDocumentInput):
    group_id: int
    text: str = Field(default="", max_length=10000)
    sender_name: str = Field(default="", max_length=500)
    source_title: str = Field(default="", max_length=500)
    contact: bool = False
    contact_name: str = Field(default="", max_length=250)
    direct: bool = False
