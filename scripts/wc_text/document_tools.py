"""Port of src/utils/documentTools.ts — the three document tools and the provider adapters."""
from typing import Any, Dict, List, Optional

CHAPTER_PARAM = {
    "type": "string",
    "description": 'Optional. The chapter to change: its position in the CHAPTER INDEX (e.g. "3" for the third chapter — never a number from its title) or its exact title. Omit to change the active chapter. Write this argument first.',
}

DOCUMENT_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "update_document",
        "description": "Replace the entire text of a chapter (the active one unless `chapter` names another), or write a new chapter (`new_chapter`). Use for a brand-new chapter, a full rewrite, or restructuring where most of the text changes. For a small change to an existing chapter, prefer edit_document. To add to a chapter or bring it up to date (new entries, a timeline carried further), change only what changes with edit_paragraphs or edit_document: a rewrite that would drop much of the text is held back.",
        "parameters": {
            "type": "object",
            "properties": {
                "chapter": CHAPTER_PARAM,
                "new_chapter": {
                    "type": "string",
                    "description": "Optional. To add a chapter: its title. The chapter is created at the end of the book and filled with `html` in this one call — there is no separate step for creating it. Leave out `chapter` when you set this. Write this argument first.",
                },
                "html": {
                    "type": "string",
                    "description": 'The COMPLETE new document as an HTML fragment: <h1>, <p>, <blockquote>, <strong>, <em>, <ul>/<ol>/<li>. No <!DOCTYPE>, <html>, <head> or <body>. Never abbreviate with placeholders like "<!-- unchanged -->". Copy every {{IMAGE_PLACEHOLDER_n}} token exactly, in place.',
                },
            },
            "required": ["html"],
        },
    },
    {
        "name": "edit_document",
        "description": "Change specific passages of a chapter (the active one unless `chapter` names another), leaving everything else untouched. Preferred for rewriting a sentence or paragraph, fixing wording, or inserting and removing a section. For another chapter, read its HTML with read first.",
        "parameters": {
            "type": "object",
            "properties": {
                "chapter": CHAPTER_PARAM,
                "edits": {
                    "type": "array",
                    "description": "One entry per separate change.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "search": {"type": "string", "description": "HTML copied EXACTLY from the current document — same tags, entities, punctuation. Include enough context to be unique. Any difference and the edit cannot be located."},
                            "replace": {"type": "string", "description": "The HTML that replaces it. Empty string deletes the passage."},
                        },
                        "required": ["search", "replace"],
                    },
                },
            },
            "required": ["edits"],
        },
    },
    {
        "name": "replace_selection",
        "description": "Rewrite ONLY the text the user currently has selected. Available when the request includes a CURRENT SELECTED TEXT section; do not use it otherwise.",
        "parameters": {
            "type": "object",
            "properties": {"html": {"type": "string", "description": "The replacement for the selected passage only, as HTML. Do not include the surrounding text."}},
            "required": ["html"],
        },
    },
]


def to_openai_tools(tools: List[Dict]) -> List[Dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]


def to_anthropic_tools(tools: List[Dict]) -> List[Dict]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]


def _clean_gemini(schema: Dict) -> Dict:
    """`toGeminiTools`' `clean`. Presence follows JavaScript truthiness: an empty
    `properties` object or `required` list IS carried (both are truthy in JS), an
    empty description is not."""
    out: Dict[str, Any] = {}
    if isinstance(schema.get("type"), str):
        out["type"] = schema["type"].upper()
    if schema.get("description"):
        out["description"] = schema["description"]
    if isinstance(schema.get("properties"), dict):
        out["properties"] = {k: _clean_gemini(v if isinstance(v, dict) else {}) for k, v in schema["properties"].items()}
    if isinstance(schema.get("items"), dict):
        out["items"] = _clean_gemini(schema["items"])
    if schema.get("required") is not None:
        out["required"] = schema["required"]
    return out


def to_gemini_tools(tools: List[Dict]) -> List[Dict]:
    return [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": _clean_gemini(t["parameters"])} for t in tools]}]


def from_openai_tools(tools: Optional[List]) -> List[Dict]:
    out = []
    for entry in tools or []:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not isinstance(fn.get("parameters"), (dict, list)):
            continue
        out.append({"name": fn["name"], "description": fn["description"] if isinstance(fn.get("description"), str) else "", "parameters": fn["parameters"]})
    return out
