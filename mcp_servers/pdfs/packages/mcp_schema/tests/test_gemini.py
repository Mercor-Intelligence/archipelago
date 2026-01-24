"""Tests for Gemini schema compatibility utilities."""

import json

import pytest
from pydantic import BaseModel

from mcp_schema import (
    GeminiBaseModel,
    GeminiSchemaGenerator,
    flatten_schema,
    get_gemini_schema,
)


class SimpleInput(BaseModel):
    """Simple input with optional field."""

    name: str
    value: int | None = None


class NestedInput(BaseModel):
    """Nested input model."""

    action: str
    file_path: str | None = None


class ComplexRequest(BaseModel):
    """Complex request with nested model."""

    session_id: str
    request: NestedInput


class TestFlattenSchema:
    """Tests for flatten_schema function."""

    def test_removes_defs(self):
        """Schema should not contain $defs."""
        schema = ComplexRequest.model_json_schema()
        assert "$defs" in json.dumps(schema)

        flattened = flatten_schema(schema)
        assert "$defs" not in json.dumps(flattened)

    def test_removes_refs(self):
        """Schema should not contain $ref."""
        schema = ComplexRequest.model_json_schema()
        assert "$ref" in json.dumps(schema)

        flattened = flatten_schema(schema)
        assert "$ref" not in json.dumps(flattened)

    def test_removes_anyof(self):
        """Schema should not contain anyOf."""
        schema = SimpleInput.model_json_schema()
        assert "anyOf" in json.dumps(schema)

        flattened = flatten_schema(schema)
        assert "anyOf" not in json.dumps(flattened)

    def test_preserves_required_fields(self):
        """Required fields should be preserved."""
        schema = flatten_schema(SimpleInput.model_json_schema())
        assert "required" in schema
        assert "name" in schema["required"]

    def test_preserves_descriptions(self):
        """Description fields should be preserved."""
        schema = flatten_schema(ComplexRequest.model_json_schema())
        # The nested model's description should be inlined
        assert "description" in schema or "description" in schema.get("properties", {}).get(
            "request", {}
        )

    def test_nested_properties_inlined(self):
        """Nested model properties should be inlined."""
        schema = flatten_schema(ComplexRequest.model_json_schema())
        request_props = schema["properties"]["request"]["properties"]
        assert "action" in request_props
        assert "file_path" in request_props


class TestGeminiSchemaGenerator:
    """Tests for GeminiSchemaGenerator class."""

    def test_generates_flat_schema(self):
        """Generator should produce flat schema."""
        schema = ComplexRequest.model_json_schema(schema_generator=GeminiSchemaGenerator)

        schema_str = json.dumps(schema)
        assert "$defs" not in schema_str
        assert "$ref" not in schema_str

    def test_handles_optional_fields(self):
        """Generator should handle Optional fields."""
        schema = SimpleInput.model_json_schema(schema_generator=GeminiSchemaGenerator)

        schema_str = json.dumps(schema)
        assert "anyOf" not in schema_str


class TestGetGeminiSchema:
    """Tests for get_gemini_schema function."""

    def test_returns_flat_schema(self):
        """Function should return flat schema."""
        schema = get_gemini_schema(ComplexRequest)

        schema_str = json.dumps(schema)
        assert "$defs" not in schema_str
        assert "$ref" not in schema_str
        assert "anyOf" not in schema_str


class TestGeminiBaseModel:
    """Tests for GeminiBaseModel class."""

    def test_inheriting_model_produces_flat_schema(self):
        """Models inheriting from GeminiBaseModel should produce flat schemas."""

        class MyInput(GeminiBaseModel):
            action: str
            file_path: str | None = None

        class MyRequest(GeminiBaseModel):
            session_id: str
            request: MyInput

        schema = MyRequest.model_json_schema()
        schema_str = json.dumps(schema)

        assert "$defs" not in schema_str
        assert "$ref" not in schema_str
        assert "anyOf" not in schema_str

    def test_model_validation_still_works(self):
        """GeminiBaseModel should still validate data correctly."""

        class MyInput(GeminiBaseModel):
            name: str
            value: int | None = None

        # Valid data
        obj = MyInput(name="test", value=42)
        assert obj.name == "test"
        assert obj.value == 42

        # Optional field
        obj2 = MyInput(name="test")
        assert obj2.value is None

        # Invalid data should raise
        with pytest.raises(Exception):
            MyInput(value=42)  # missing required field

    def test_model_serialization_works(self):
        """GeminiBaseModel should serialize correctly."""

        class MyInput(GeminiBaseModel):
            name: str
            value: int | None = None

        obj = MyInput(name="test", value=42)
        data = obj.model_dump()

        assert data == {"name": "test", "value": 42}


class TestRecursiveSchema:
    """Tests for handling recursive schemas."""

    def test_handles_recursive_reference(self):
        """Should handle recursive model references without infinite loop."""

        class TreeNode(BaseModel):
            name: str
            children: list["TreeNode"] | None = None

        # This would normally cause infinite recursion
        schema = flatten_schema(TreeNode.model_json_schema())

        # Should complete without hanging
        schema_str = json.dumps(schema)
        assert "$defs" not in schema_str
        assert "$ref" not in schema_str
