from pydantic import BaseModel, Field, field_validator


class FullNameFields(BaseModel):
    full_name: str | None = Field(default=None, max_length=160)

    @field_validator("full_name")
    @classmethod
    def normalize_full_name(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None
