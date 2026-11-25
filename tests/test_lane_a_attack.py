"""Lane A acceptance tests. They switch on as A removes each @stub. Add more as you build."""

import pytest
from agents import RunContextWrapper

from gauntlet import adapter, config
from gauntlet.attack import fanout, planner, recon, refine
from gauntlet.schemas import Attack, Category, Outcome, TargetConfig, TargetProfile, Transcript, Turn, Verdict
from gauntlet.state import ScanState
from gauntlet.stubs import NotBuiltYet
from tests.conftest import needs


def _state():
    return ScanState(target=TargetConfig(kind="zoo", name="leaky"), mode="quick")


@needs(planner.plan_attacks)
async def test_planner_returns_per_category_attacks():
    cats = [Category.DATA_LEAK, Category.OFF_TASK]
    attacks = await planner.plan_attacks(_state(), cats, 2)
    assert len(attacks) == 4 and {a.category for a in attacks} == set(cats)
    assert len({a.id for a in attacks}) == 4


@needs(fanout.run_all)
async def test_blocked_reply_ends_the_run(monkeypatch):
    async def blocked(target, history, message):
        return Turn(role="target", content="", blocked=True, finish_reason="content_filter")

    monkeypatch.setattr(adapter, "send", blocked)
    attack = Attack(id="a1", category=Category.DATA_LEAK, goal="g", opening_message="hi")
    (t,) = (await fanout.run_all(_state(), [attack], 1))["a1"]
    assert t.provider_blocked and len(t.turns) == 2


@needs(fanout.run_all)
async def test_one_failing_run_does_not_kill_the_others(monkeypatch):
    calls = {"n": 0}

    async def flaky(target, history, message):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return Turn(role="target", content="no")

    monkeypatch.setattr(adapter, "send", flaky)
    attack = Attack(id="a1", category=Category.DATA_LEAK, goal="g", opening_message="hi")
    runs = (await fanout.run_all(_state(), [attack], 3))["a1"]
    assert len(runs) == 3 and sum(1 for r in runs if r.error) == 1


# ---------- added by A ----------

def _attack():
    return Attack(id="a1", category=Category.SYSTEM_PROMPT_LEAK, goal="g", opening_message="hi", strategy="s")


async def _says_no(target, history, message):
    return Turn(role="target", content="no")


@needs(recon.run_recon)
async def test_recon_asks_questions_and_sets_the_profile(monkeypatch):
    asked = []

    async def answer(target, history, message):
        asked.append(message)
        return Turn(role="target", content="I help with shoe orders.")

    monkeypatch.setattr(adapter, "send", answer)
    state = _state()
    profile = await recon.run_recon(state)
    assert asked == recon.QUESTIONS
    assert state.profile is profile and "shoe" in profile.purpose.lower()


@needs(recon.run_recon)
async def test_recon_raises_when_the_target_is_unreachable(monkeypatch):
    async def down(target, history, message):
        raise adapter.TargetError("HTTP 502")

    monkeypatch.setattr(adapter, "send", down)
    with pytest.raises(adapter.TargetError):
        await recon.run_recon(_state())


@needs(planner.plan_attacks)
def test_planner_instructions_are_built_from_the_profile():
    state = _state()
    state.profile = TargetProfile(purpose="Sells running shoes", tools_mentioned=["order_lookup"])
    text = planner.build_instructions(RunContextWrapper(context=state), None)
    assert "Sells running shoes" in text and "order_lookup" in text


@needs(planner.plan_attacks)
def test_planner_tops_up_short_categories_from_the_library():
    got = [planner.PlannedAttack(category=Category.OFF_TASK, goal="g", opening_message="write me a poem")]
    attacks, topped_up = planner.assemble(got, [Category.OFF_TASK, Category.DATA_LEAK], 2)
    assert len(attacks) == 4 and topped_up == 3
    assert [a.id for a in attacks] == ["off_task-1", "off_task-2", "data_leak-1", "data_leak-2"]
    assert attacks[0].opening_message == "write me a poem"


@needs(fanout.run_all)
async def test_conversation_runs_max_turns_when_the_target_holds(monkeypatch):
    monkeypatch.setattr(adapter, "send", _says_no)
    (t,) = (await fanout.run_all(_state(), [_attack()], 1))["a1"]
    assert len(t.turns) == 2 * config.MAX_TURNS and not t.error
    assert t.turns[0].content == "hi" and [x.role for x in t.turns[:2]] == ["attacker", "target"]


@needs(fanout.run_all)
async def test_canary_hit_stops_the_run_early(monkeypatch):
    async def leaks(target, history, message):
        return Turn(role="target", content="sure, the code is SECRET-123")

    monkeypatch.setattr(adapter, "send", leaks)
    monkeypatch.setattr(fanout, "find_canary", lambda target, text: "SECRET-123" if "SECRET-123" in text else None)
    (t,) = (await fanout.run_all(_state(), [_attack()], 1))["a1"]
    assert len(t.turns) == 2


@needs(fanout.run_all)
async def test_hooks_stream_every_turn_live(monkeypatch):
    monkeypatch.setattr(adapter, "send", _says_no)
    state = _state()
    await fanout.run_all(state, [_attack()], 2)
    turns = []
    while not state.events.empty():
        e = state.events.get_nowait()
        if e.data.get("kind") == "turn":
            turns.append(e.data)
    assert len(turns) == 2 * 2 * config.MAX_TURNS  # 2 runs x (attacker + target) per turn
    assert {t["role"] for t in turns} == {"attacker", "target"} and {t["run"] for t in turns} == {0, 1}


@needs(fanout.run_all)
async def test_out_of_time_skips_new_runs(monkeypatch):
    monkeypatch.setattr(adapter, "send", _says_no)
    state = _state()
    state.deadline_s = 0
    (t,) = (await fanout.run_all(state, [_attack()], 1))["a1"]
    assert not t.turns and "time budget" in t.error


@needs(fanout.run_all)
async def test_an_unbuilt_step_is_reported_not_hidden(monkeypatch):
    async def unbuilt(target, history, message):
        raise NotBuiltYet("[C] Victim zoo")

    monkeypatch.setattr(adapter, "send", unbuilt)
    with pytest.raises(NotBuiltYet):
        await fanout.run_all(_state(), [_attack()], 2)


@needs(refine.refine)
async def test_refine_keeps_the_goal_and_links_the_retry():
    t = Transcript(attack_id="a1", run_index=0, turns=[Turn(role="attacker", content="hi"), Turn(role="target", content="no")])
    v = Verdict(attack_id="a1", run_index=0, outcome=Outcome.HELD, score=0, reason="It refused.")
    new = await refine.refine(_state(), _attack(), t, v, 1)
    assert new.id == "a1-r1" and new.retry_of == "a1" and new.goal == "g" and new.category == Category.SYSTEM_PROMPT_LEAK
