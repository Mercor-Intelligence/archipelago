from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
DescriptorId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
Conversion = Literal["identity", "milliseconds", "fraction", "tokens", "prepend"]
DescriptorGroup = Literal["Declaration", "Knob", "Emitter"]
ValueType = Literal["number", "integer", "boolean", "string", "select", "json"]
TextKey = Annotated[Text, PlainSerializer(str, return_type=str)]
DescriptorKey = Annotated[DescriptorId, PlainSerializer(str, return_type=str)]

_FIELD_TYPES_BY_VALUE_TYPE: dict[ValueType, tuple[str, ...]] = {
    "number": ("number",),
    "integer": ("number",),
    "boolean": ("boolean",),
    "string": ("text", "textarea"),
    "select": ("select",),
    "json": ("json",),
}
_OVERRIDE_FIELD_TYPES_BY_VALUE_TYPE = {
    **_FIELD_TYPES_BY_VALUE_TYPE,
    "string": ("text", "textarea", "select"),
}


class ParameterOverride(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    field_schema: dict[str, JsonValue] = Field(default_factory=dict)
    runtime_default: JsonValue = None
    on_missing: Literal["omit", "default"] = "omit"
    value_unit: Literal["fraction", "tokens"] | None = None
    legacy_keys: dict[TextKey, Conversion] = Field(default_factory=dict)


class DescriptorDefinition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    label: Text
    group: DescriptorGroup
    is_core: bool
    value_type: ValueType
    meaning: Text
    field_schema: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_field_type(self) -> Self:
        if "field_type" not in self.field_schema:
            return self

        field_type = self.field_schema["field_type"]
        if field_type not in _FIELD_TYPES_BY_VALUE_TYPE[self.value_type]:
            raise ValueError(
                f"field_schema.field_type {field_type!r} is incompatible "
                f"with value_type {self.value_type!r}"
            )
        return self


class ParameterAlias(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    descriptor_id: DescriptorId
    conversion: Conversion = "identity"


class HarnessDescriptors(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    parameters: dict[DescriptorKey, ParameterOverride] = Field(default_factory=dict)


class DescriptorCatalog(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    format: Literal["studio.harness_descriptors.v1"] = "studio.harness_descriptors.v1"
    descriptors: dict[str, DescriptorDefinition]
    harnesses: dict[str, HarnessDescriptors] = Field(default_factory=dict)

    @field_validator("descriptors")
    @classmethod
    def validate_ids(
        cls, values: dict[str, DescriptorDefinition]
    ) -> dict[str, DescriptorDefinition]:
        TypeAdapter(list[DescriptorId]).validate_python(list(values))
        return values

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for harness, metadata in self.harnesses.items():
            unknown = set(metadata.parameters) - self.descriptors.keys()
            if unknown:
                raise ValueError(
                    f"Unknown descriptor IDs for {harness}: {sorted(unknown)}"
                )
            for descriptor_id, override in metadata.parameters.items():
                if "field_type" not in override.field_schema:
                    continue
                field_type = override.field_schema["field_type"]
                value_type = self.descriptors[descriptor_id].value_type
                if field_type not in _OVERRIDE_FIELD_TYPES_BY_VALUE_TYPE[value_type]:
                    raise ValueError(
                        f"Harness override field_schema.field_type {field_type!r} "
                        f"is incompatible with descriptor {descriptor_id!r} "
                        f"value_type {value_type!r} for {harness}"
                    )
        return self


class DescriptorValue(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    value: JsonValue
    source_key: Text
    value_unit: Literal["fraction", "tokens"] | None = None


class HarnessDescriptorValues(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True)

    harness_id: Text
    values: dict[DescriptorKey, DescriptorValue] = Field(default_factory=dict)
    unmapped_keys: list[str] = Field(default_factory=list)
    issues: dict[str, str] = Field(default_factory=dict)
