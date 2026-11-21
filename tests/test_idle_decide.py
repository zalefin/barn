import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "idle_decide", Path(__file__).parent.parent / "src" / "barn" / "bootstrap" / "idle_decide.py"
)
idle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idle)

H = 3600
NOW = 1_000_000.0
BOOT = "boot-1"


def quiet():
    return {"herdr_error": False, "panes": {}, "sessions": [], "foreground": [], "hold_until": 0, "boot_id": BOOT}


def test_first_run_starts_the_clock():
    action, reasons, state = idle.decide(NOW, quiet(), {}, 4)
    assert action == "none" and reasons == []
    assert state["last_active"] == NOW


def test_idle_window_then_warn_then_shutdown():
    state = {"boot_id": BOOT, "last_active": NOW - 4 * H}
    action, _, state = idle.decide(NOW, quiet(), state, 4)
    assert action == "warn"
    action, _, state = idle.decide(NOW + 10 * 60, quiet(), state, 4)
    assert action == "none"  # still inside the 15 minute warning
    action, _, state = idle.decide(NOW + 15 * 60, quiet(), state, 4)
    assert action == "shutdown"


def test_activity_after_warning_cancels_it():
    state = {"boot_id": BOOT, "last_active": NOW - 5 * H, "warned_at": NOW - 10 * 60}
    inputs = quiet() | {"panes": {"w1:p1": "working"}}
    action, _, state = idle.decide(NOW, inputs, state, 4)
    assert action == "none" and state["warned_at"] is None and state["last_active"] == NOW


def test_not_idle_long_enough():
    action, _, _ = idle.decide(NOW, quiet(), {"boot_id": BOOT, "last_active": NOW - 3 * H}, 4)
    assert action == "none"


def test_state_from_an_earlier_boot_is_discarded():
    # A box that idle-stopped and was started again: the old clock and the old
    # warning must not shut it down on the first check after boot.
    stale = {"boot_id": "boot-0", "last_active": NOW - 4 * 24 * H, "warned_at": NOW - 4 * H, "blocked_since": {"p": 0}}
    action, _, state = idle.decide(NOW, quiet(), stale, 4)
    assert action == "none"
    assert state == {"boot_id": BOOT, "last_active": NOW, "warned_at": None, "blocked_since": {}}
    # The new boot gets a full idle window, then the warning, then shutdown.
    action, _, state = idle.decide(NOW + 4 * H, quiet(), state, 4)
    assert action == "warn"
    action, _, state = idle.decide(NOW + 4 * H + 15 * 60, quiet(), state, 4)
    assert action == "shutdown"


def test_state_without_boot_id_is_discarded():
    # State written before barn recorded the boot id.
    stale = {"last_active": NOW - 4 * 24 * H, "warned_at": NOW - 4 * H, "blocked_since": {}}
    action, _, state = idle.decide(NOW, quiet(), stale, 4)
    assert action == "none" and state["last_active"] == NOW and state["warned_at"] is None


def test_stale_warning_is_given_again_not_acted_on():
    # Same boot, but the warning is far older than one warning lead: warn again.
    state = {"boot_id": BOOT, "last_active": NOW - 6 * H, "warned_at": NOW - 2 * H}
    action, _, state = idle.decide(NOW, quiet(), state, 4)
    assert action == "warn" and state["warned_at"] == NOW
    action, _, state = idle.decide(NOW + 15 * 60, quiet(), state, 4)
    assert action == "shutdown"


def test_working_agent_is_active():
    reasons, _ = idle.active_reasons(NOW, quiet() | {"panes": {"w1:p1": "working"}}, {})
    assert reasons == ["agent working in w1:p1"]


def test_idle_and_done_agents_are_not_active():
    reasons, _ = idle.active_reasons(NOW, quiet() | {"panes": {"a": "idle", "b": "done", "c": "unknown"}}, {})
    assert reasons == []


def test_blocked_agent_active_for_24h_only():
    inputs = quiet() | {"panes": {"w1:p2": "blocked"}}
    reasons, blocked = idle.active_reasons(NOW, inputs, {})
    assert reasons and blocked == {"w1:p2": NOW}
    reasons, blocked = idle.active_reasons(NOW + 23 * H, inputs, {"blocked_since": blocked})
    assert reasons and blocked == {"w1:p2": NOW}
    reasons, blocked = idle.active_reasons(NOW + 24 * H, inputs, {"blocked_since": blocked})
    assert reasons == [] and blocked == {"w1:p2": NOW}


def test_blocked_clock_resets_when_pane_unblocks():
    _, blocked = idle.active_reasons(NOW, quiet() | {"panes": {"p": "working"}}, {"blocked_since": {"p": 0}})
    assert blocked == {}


def test_herdr_error_fails_safe():
    reasons, _ = idle.active_reasons(NOW, quiet() | {"herdr_error": True}, {})
    assert reasons


def test_recent_tty_session_is_active_old_one_is_not():
    fresh = {"id": "3", "class": "user", "tty": "pts/0", "idle_since": NOW - 10 * 60}
    stale = {"id": "4", "class": "user", "tty": "pts/1", "idle_since": NOW - 31 * 60}
    assert idle.active_reasons(NOW, quiet() | {"sessions": [fresh]}, {})[0]
    assert not idle.active_reasons(NOW, quiet() | {"sessions": [stale]}, {})[0]


def test_sessions_without_tty_or_not_user_class_ignored():
    notty = {"id": "3", "class": "user", "tty": "", "idle_since": NOW}
    manager = {"id": "c1", "class": "manager", "tty": "pts/0", "idle_since": NOW}
    assert not idle.active_reasons(NOW, quiet() | {"sessions": [notty, manager]}, {})[0]


def test_foreground_non_agent_process_is_active():
    assert idle.active_reasons(NOW, quiet() | {"foreground": ["make"]}, {})[0]
    assert not idle.active_reasons(NOW, quiet() | {"foreground": ["claude"]}, {})[0]


def test_hold():
    assert idle.active_reasons(NOW, quiet() | {"hold_until": NOW + 60}, {})[0] == ["hold"]
    assert not idle.active_reasons(NOW, quiet() | {"hold_until": NOW - 60}, {})[0]


def test_parse_herdr_snapshot_finds_nested_panes():
    doc = {
        "result": {
            "type": "session_snapshot",
            "workspaces": [{"tabs": [{"panes": [{"pane_id": "w1:p1", "agent_status": "working"}]}]}],
            "panes": [{"pane_id": "w1:p2", "agent_status": "blocked"}, {"pane_id": "w1:p3"}],
        }
    }
    assert idle.parse_herdr_snapshot(json.dumps(doc)) == {"w1:p1": "working", "w1:p2": "blocked"}


def test_parse_sessions():
    text = "3\tuser\tpts/0\t1700000000000000\nc1\tmanager\t\t0\nbad line\n"
    s = idle.parse_sessions(text)
    assert s == [
        {"id": "3", "class": "user", "tty": "pts/0", "idle_since": 1_700_000_000.0},
        {"id": "c1", "class": "manager", "tty": "", "idle_since": 0.0},
    ]


def test_gather_and_main(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "herdr.rc").write_text("absent\n")
    (d / "hold-until").write_text(f"{int(NOW) + 100}\n")
    (d / "boot-id").write_text("0f7e2c1a-boot\n")
    state = tmp_path / "state.json"
    assert idle.main(["x", str(d), str(state), "4", str(NOW)]) == 0
    saved = json.loads(state.read_text())
    assert saved["last_active"] == NOW and saved["boot_id"] == "0f7e2c1a-boot"


def test_gather_herdr_failure(tmp_path):
    (tmp_path / "herdr.rc").write_text("1\n")
    assert idle.gather(tmp_path)["herdr_error"] is True
    (tmp_path / "herdr.rc").write_text("0\n")
    (tmp_path / "herdr.json").write_text("not json")
    assert idle.gather(tmp_path)["herdr_error"] is True
