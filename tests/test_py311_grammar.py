"""Hermes supports Python >= 3.11, but the test machines may run 3.12+, whose f-string grammar (PEP 701) accepts code that
3.11 rejects (the same quote reused inside an f-string expression, a backslash or comment inside one). Catch that here."""

import ast
import io
import pathlib
import sys
import tokenize

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def repo_py_files():
    return sorted(p for p in ROOT.rglob("*.py") if not {"node_modules", ".git"} & set(p.relative_to(ROOT).parts))


def test_every_file_parses_with_the_3_11_grammar():
    assert repo_py_files()
    for p in repo_py_files():
        ast.parse(p.read_text(), str(p), feature_version=(3, 11))


@pytest.mark.skipif(sys.version_info < (3, 12), reason="3.11 itself rejects these at compile time (the parse test above)")
def test_no_pep701_only_fstrings():
    bad = []
    for p in repo_py_files():
        stack = []                       # quote chars of the f-strings we are currently inside
        for tok in tokenize.generate_tokens(io.StringIO(p.read_text()).readline):
            name = tokenize.tok_name[tok.type]
            if name == "FSTRING_START":
                q = tok.string.lstrip("fFrRbB")
                if stack and stack[-1] == q:
                    bad.append(f"{p.relative_to(ROOT)}:{tok.start[0]} reuses {q} inside an f-string")
                stack.append(q)
            elif name == "FSTRING_END":
                stack.pop()
            elif stack and name == "STRING" and tok.string.lstrip("rRbBuU")[:1] == stack[-1][:1] and len(stack[-1]) == 1:
                bad.append(f"{p.relative_to(ROOT)}:{tok.start[0]} reuses {stack[-1]} inside an f-string")
            elif stack and name == "COMMENT":
                bad.append(f"{p.relative_to(ROOT)}:{tok.start[0]} comment inside an f-string expression")
    assert not bad, bad
