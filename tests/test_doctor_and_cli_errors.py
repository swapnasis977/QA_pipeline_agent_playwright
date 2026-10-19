"""`doctor` must find setup problems up front - and must never print a secret.

Failures these prevent:
  - A missing API key surfaced as exit 5 halfway through a run, after the work had
    started; a missing `npm install` as a Playwright run that failed every test.
  - The output of a diagnostic command gets pasted into tickets. `.env` holds real
    credentials, so the command must show NAMES and yes/no, never a value.
  - An unexpected exception printed a raw traceback at someone who just wanted to run
    a command; a profile named "Example App" broke shell quoting and CI paths.
"""

import pytest

from qa_pipeline import cli, doctor

SECRET = "sk-ant-THIS-MUST-NEVER-BE-PRINTED"


def _status(results, label):
    return next(r for r in results if r[0] == label)[1]


def test_a_missing_model_key_is_a_failure_that_names_the_variable():
    status, detail, fix = doctor.check_model_key({"AI_PROVIDER": "anthropic"})
    assert status == doctor.FAIL and "ANTHROPIC_API_KEY" in detail and "ANTHROPIC_API_KEY" in fix


def test_each_provider_checks_its_own_variables():
    assert doctor.check_model_key({"AI_PROVIDER": "openai"})[0] == doctor.FAIL
    assert doctor.check_model_key({"AI_PROVIDER": "openai", "OPENAI_API_KEY": "x"})[0] == doctor.OK
    assert doctor.check_model_key({"AI_PROVIDER": "ollama"})[0] == doctor.OK     # needs no key


def test_an_unknown_provider_is_a_failure():
    assert doctor.check_model_key({"AI_PROVIDER": "gpt-banana"})[0] == doctor.FAIL


def test_no_secret_value_ever_appears_in_the_output():
    env = {"AI_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": SECRET,
           "LOCAL_APP_TEST_PASSWORD": SECRET, "JIRA_API_TOKEN": SECRET,
           "LOCAL_APP_BASE_URL": "https://x", "LOCAL_APP_TEST_USER": "u"}
    text = doctor.render(doctor.run_checks(None, env=env))
    assert SECRET not in text
    assert "JIRA_API_TOKEN" in text          # the NAME is shown


def test_the_application_login_check_follows_test_env(monkeypatch):
    monkeypatch.setenv("TEST_ENV", "central")
    env = {"LOCAL_APP_BASE_URL": "x", "LOCAL_APP_TEST_USER": "x", "LOCAL_APP_TEST_PASSWORD": "x"}
    assert doctor.check_app_env(env)[0] == doctor.WARN       # central is selected, only local is set
    assert "CENTRAL_APP_BASE_URL" in doctor.check_app_env(env)[1]


def test_one_failure_gives_a_non_zero_exit_code():
    assert doctor.exit_code([("a", doctor.OK, "", ""), ("b", doctor.FAIL, "", "")]) == 1
    assert doctor.exit_code([("a", doctor.OK, "", ""), ("b", doctor.WARN, "", "")]) == 0


def test_a_check_that_crashes_does_not_hide_the_others(monkeypatch):
    monkeypatch.setattr(doctor, "check_node", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    results = doctor.run_checks(None, env={})
    assert _status(results, "Node.js") == doctor.WARN
    assert len(results) >= 8


@pytest.mark.parametrize("name,ok", [("myapp", True), ("example_app-x", True), ("Example App", False),
                                     ("My App", False), ("-x", False), ("a/b", False)])
def test_profile_names_are_safe_for_shells_and_paths(name, ok):
    assert bool(doctor.VALID_NAME.match(name)) is ok


def test_init_profile_refuses_a_name_with_spaces_and_suggests_one(capsys):
    code = cli.cmd_init_profile(type("A", (), {"name": "Example App"})())
    out = capsys.readouterr().out
    assert code == 1 and "example_app" in out


def test_help_lists_the_exit_codes(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "6 tests still failing" in out and "doctor" in out


def test_an_unexpected_error_is_a_short_message_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_contracts", lambda a: (_ for _ in ()).throw(ZeroDivisionError("oops")))
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    parser_args = cli.build_parser().parse_args(["contracts"])
    parser_args.func = cli.cmd_contracts
    monkeypatch.setattr(cli.build_parser().__class__, "parse_args", lambda self, argv=None: parser_args)

    assert cli.main(["contracts"]) == 1
    out = capsys.readouterr().out
    assert "[ERROR] ZeroDivisionError: oops" in out and "--debug" in out and "Traceback" not in out
