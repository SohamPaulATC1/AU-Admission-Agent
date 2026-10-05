"""Offline simulator for the course catalog tools (spec section 8).

Calls the same course_catalog.handle_tool_call as app.py, so what it prints is
exactly what Gemini receives. Not named test_*, so unittest discovery skips it.

    venv312/Scripts/python.exe tests/tool_playground.py                         # interactive
    venv312/Scripts/python.exe tests/tool_playground.py list_programs degree=B.Tech
    venv312/Scripts/python.exe tests/tool_playground.py get_program_details programs=cse,ece fields=fees
    venv312/Scripts/python.exe tests/tool_playground.py get_program_details "{\"programs\": [\"ai ml\"]}"
    venv312/Scripts/python.exe tests/tool_playground.py --declarations
"""
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import course_catalog  # noqa: E402

SPECS = {spec["name"]: spec for spec in course_catalog.TOOL_SPECS}


def parse_args(tool, items):
    """key=value pairs (commas split array args) or a single raw JSON object."""
    if len(items) == 1 and items[0].lstrip().startswith("{"):
        return json.loads(items[0])
    properties = SPECS[tool]["parameters"]["properties"] if tool in SPECS else {}
    args = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"expected key=value, got {item!r}")
        kind = properties.get(name, {}).get("type")
        if kind == "ARRAY":
            args[name] = [v.strip() for v in value.split(",") if v.strip()]
        elif kind in ("INTEGER", "NUMBER"):
            try:
                args[name] = int(value)
            except ValueError:
                args[name] = value
        else:
            args[name] = value
    return args


def show(tool, args):
    response = course_catalog.handle_tool_call(tool, args)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    chars = course_catalog.response_chars(response)
    print(f"-- {chars} chars, about {chars // 4} tokens")


def show_declarations():
    print(json.dumps(course_catalog.TOOL_SPECS, indent=2, ensure_ascii=False))
    chars = course_catalog.response_chars(course_catalog.TOOL_SPECS)
    print(f"-- {chars} chars, about {chars // 4} tokens")


def interactive():
    names = sorted(SPECS)
    print("Tools: " + ", ".join(f"{i + 1}) {n}" for i, n in enumerate(names)) + ". Empty line exits.")
    while True:
        try:
            choice = input("tool> ").strip()
            if not choice:
                return
            tool = names[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(names) else choice
            line = input("args> ").strip()
        except EOFError:
            return
        try:
            args = parse_args(tool, [line] if line.startswith("{") else shlex.split(line))
        except ValueError as e:
            print(f"!! {e}")
            continue
        show(tool, args)


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        interactive()
    elif argv[0] == "--declarations":
        show_declarations()
    else:
        show(argv[0], parse_args(argv[0], argv[1:]))


if __name__ == "__main__":
    main(sys.argv[1:])
