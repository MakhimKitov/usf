"""The role verbs, driven the way an operator drives them (D5, #31).

Through ``build_parser`` + the ``_role`` handler rather than ``main()`` (which
loads the deployment's .env). The store contract — both backends, the trigger —
is `test_ledger_contract.py`'s and `test_schema_walls.py`'s; the engine seam
("the next burst changes") is `test_roles.py`'s. This file is the operator
surface: what `list`/`show` say, what `set` changes, what it refuses and how.
"""

from control_plane import cli
from control_plane.store import SqliteLedger


def run(tmp_path, *argv):
    args = cli.build_parser().parse_args(["role", *argv, "--data-dir", str(tmp_path), "--actor", "op"])
    return cli._role(args)


def stored(tmp_path, role):
    ledger = SqliteLedger(tmp_path / "ledger.sqlite3")
    try:
        return ledger.get_role(role)
    finally:
        ledger.close()


def test_list_names_the_four_slots_with_their_open_fields(tmp_path, capsys):
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == ["dev", "monitor", "reviewer", "tester"]
    assert (
        "dev  pipeline  model=claude-sonnet-5  effort=default  budget=$2.00  turns=100  mount=rw  preamble=no"
    ) in out


def test_set_changes_the_open_fields_and_says_so(tmp_path, capsys):
    preamble = tmp_path / "reviewer-preamble.md"
    preamble.write_text("Correctness first; be terse.\n", encoding="utf-8")
    assert run(
        tmp_path, "set", "reviewer",
        "--model", "claude-opus-5", "--budget", "0.5", "--turns", "30",
        "--preamble-file", str(preamble), "--reason", "cheaper reviews",
    ) == 0
    out = capsys.readouterr().out
    assert "reviewer set: max_budget_usd, max_turns, model, prompt_preamble — governs the next burst" in out
    assert "model:     claude-opus-5" in out and "budget:    $0.50   turns: 30   mount: ro" in out
    row = stored(tmp_path, "reviewer")
    assert (row.model, row.max_budget_usd, row.max_turns) == ("claude-opus-5", 0.5, 30)
    assert row.prompt_preamble == "Correctness first; be terse.\n"

    assert run(tmp_path, "show", "reviewer") == 0
    out = capsys.readouterr().out
    assert "(frozen)" in out  # the wall fields are shown as what they are
    assert "#1 " in out and "op role.set — cheaper reviews" in out


def test_set_clears_a_preamble_with_an_empty_string(tmp_path):
    assert run(tmp_path, "set", "dev", "--preamble", "Be terse.") == 0
    assert stored(tmp_path, "dev").prompt_preamble == "Be terse."
    assert run(tmp_path, "set", "dev", "--preamble", "") == 0
    assert stored(tmp_path, "dev").prompt_preamble == ""


def test_set_effort_and_back_to_the_models_default(tmp_path, capsys):
    assert run(tmp_path, "set", "dev", "--effort", "xhigh", "--reason", "think harder") == 0
    out = capsys.readouterr().out
    assert "dev set: effort — governs the next burst" in out
    assert "model:     claude-sonnet-5   effort: xhigh" in out
    assert stored(tmp_path, "dev").effort == "xhigh"
    assert stored(tmp_path, "reviewer").effort is None

    assert run(tmp_path, "set", "dev", "--effort", "default") == 0
    assert "effort: default" in capsys.readouterr().out
    assert stored(tmp_path, "dev").effort is None


def test_an_unknown_effort_refuses_at_the_flag(tmp_path, capsys):
    import pytest

    with pytest.raises(SystemExit):
        run(tmp_path, "set", "dev", "--effort", "ludicrous")
    assert "invalid choice: 'ludicrous'" in capsys.readouterr().err
    assert stored(tmp_path, "dev").effort is None


def test_set_refuses_what_it_must(tmp_path, capsys):
    # nothing named
    assert run(tmp_path, "set", "dev") == 2
    assert "nothing to set" in capsys.readouterr().err
    # two spellings of one setting
    assert run(tmp_path, "set", "dev", "--preamble", "x", "--preamble-file", str(tmp_path / "p")) == 2
    assert "one setting" in capsys.readouterr().err
    # a value the column cannot take
    assert run(tmp_path, "set", "dev", "--budget", "0") == 2
    assert "must be positive" in capsys.readouterr().err
    # an unknown role
    assert run(tmp_path, "set", "ghost", "--model", "m") == 2
    assert "no role named 'ghost'" in capsys.readouterr().err
    assert run(tmp_path, "show", "ghost") == 2
    # nothing above left a trail or a change
    assert stored(tmp_path, "dev").max_budget_usd == 2.0


def test_the_wall_fields_have_no_flag(tmp_path):
    """The operator surface offers no way to spell a wall-field edit: the
    parser has no --tools/--identity/--sink/--kind. (The store and the trigger
    refuse them by name for anyone who reaches past the CLI.)"""
    import pytest

    for flag in ("--tools", "--identity", "--sink", "--kind"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["role", "set", "dev", flag, "x"])
