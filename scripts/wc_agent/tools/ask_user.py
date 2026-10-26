"""Port of src/agent/tools/askUser.ts — a question the run waits on."""
from typing import Any, Dict, Union

from wc_text.jsstr import js_trim

from ..registry import Tool
from ..types import ToolContext, result


def _parse(raw) -> Union[Dict[str, Any], str]:
    question = js_trim(raw["question"]) if raw and isinstance(raw.get("question"), str) else ""
    if not question:
        return "the question was empty"
    options = [js_trim(o) for o in raw.get("options", []) if isinstance(o, str) and js_trim(o)][:4] if raw and isinstance(raw.get("options"), list) else []
    return {"question": question, "options": options}


async def _execute(args: Dict[str, Any], ctx: ToolContext, call: Dict[str, Any]) -> Dict[str, Any]:
    ctx.run.question = {"question": args["question"], "options": args["options"]}
    opts = f" [{' / '.join(args['options'])}]" if args["options"] else ""
    return result(True, "The question is shown to the user. Their answer arrives as the next message; wait for it — this turn ends here.",
                  f"❓ asked: {args['question']}{opts}")


ask_user_tool = Tool(
    name="ask_user",
    description=("Ask the user one question and wait for the answer. Use it only when the answer changes what you would do: the request can be read two ways, or the next step is hard to undo (restructuring the outline, discarding a draft). delete_chapter asks for itself; do not ask before calling it. "
                 'Never use it to ask permission for ordinary work, to confirm an obvious next step, or to announce progress. Put the choice you recommend first and end its label with "(Recommended)"; the user can also type their own answer.'),
    parameters={"type": "object", "properties": {
        "question": {"type": "string", "description": "The question, in the language of the conversation."},
        "options": {"type": "array", "description": "Two to four short choices; the recommended one first. Optional.", "items": {"type": "string"}},
    }, "required": ["question"]},
    kind="read", parse=_parse, execute=_execute,
)
