from core.server import server
from core.tool_registry import get_tool_components
import gmail.gmail_tools  # noqa: F401


def _assert_publishes_plain_string_array(schema, tool_name):
    """
    The optional label-id arrays must publish a plain `array` schema.

    Two independent constraints meet on these fields:

    * Clients that don't resolve `anyOf` need the parent `type`/`items` pair to
      infer an array at all (upstream issue 611).
    * Moonshot (Kimi K3, via OpenRouter) validates tool schemas strictly and
      rejects the whole request — 400, no tokens generated — when a property
      carries `anyOf` alongside a sibling `type`.

    A bare `StringList` defaulting via `default_factory` satisfies both: parent
    `type` + `items`, no `anyOf`, and the field stays out of `required`.
    """
    for field_name in ("add_label_ids", "remove_label_ids"):
        field_schema = schema[field_name]
        assert field_schema["type"] == "array", tool_name
        assert field_schema["items"] == {"type": "string"}, tool_name
        # No union keyword may appear beside the parent `type`.
        for keyword in ("anyOf", "oneOf", "allOf"):
            assert keyword not in field_schema, (
                f"{tool_name}.{field_name} published {keyword} beside a parent "
                "`type`; Moonshot rejects that shape"
            )


def test_modify_gmail_message_labels_optional_arrays_publish_array_type():
    components = get_tool_components(server)
    tool = components["modify_gmail_message_labels"]

    _assert_publishes_plain_string_array(
        tool.parameters["properties"], "modify_gmail_message_labels"
    )
    # Defaulted, so callers may omit them.
    assert "add_label_ids" not in tool.parameters.get("required", [])
    assert "remove_label_ids" not in tool.parameters.get("required", [])


def test_batch_modify_gmail_message_labels_optional_arrays_publish_array_type():
    components = get_tool_components(server)
    tool = components["batch_modify_gmail_message_labels"]

    _assert_publishes_plain_string_array(
        tool.parameters["properties"], "batch_modify_gmail_message_labels"
    )
    assert "add_label_ids" not in tool.parameters.get("required", [])
    assert "remove_label_ids" not in tool.parameters.get("required", [])
