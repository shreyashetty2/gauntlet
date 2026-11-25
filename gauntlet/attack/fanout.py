"""Parallel multi-turn attacks. Owner: A. Issues: "[A] Multi-turn attacker + fan-out", "[A] Live progress hooks".

Concept: Parallel fan-out. Run every attack k times at once (asyncio.gather).
Each run is a multi-turn conversation of up to config.MAX_TURNS: the attacker
agent writes the next message from the target's last reply; the target answers
through gauntlet.adapter.send.

Concept: Hooks. Use a RunHooks subclass to emit live progress events
(state.emit) for each attacker turn, so the browser sees attacks as they happen.

Rules:
- Use gauntlet.models.run_agent (semaphore, retries, block detection).
- A target Turn with blocked=True ends the run: set transcript.provider_blocked.
- Stop early if gauntlet.judge.canary.find_canary hits (leak proven).
- Stop starting new turns when state.out_of_time().
- An exception in one run sets transcript.error; it must not kill the others.

Returns {attack_id: [Transcript, ...k]}.
"""

import asyncio
from typing import Any

from agents import Agent, RunHooks

from gauntlet import adapter, config, models
from gauntlet.judge.canary import find_canary
from gauntlet.schemas import Attack, Transcript, Turn
from gauntlet.state import ScanState
from gauntlet.stubs import NotBuiltYet
from gauntlet.tracing import observe

PREVIEW_CHARS = 240


class AttackProgressHooks(RunHooks[ScanState]):
    """Streams one run's conversation to the browser as it happens.

    The attacker agent's messages arrive through on_agent_end; the opening
    message and the target's replies come from the fan-out loop via turn().
    Every event is a "status" event with kind="turn", so the UI can show a live feed.
    """

    def __init__(self, state: ScanState, attack: Attack, run_index: int):
        self.state, self.attack, self.run_index = state, attack, run_index
        self.turn_no = 0

    def turn(self, role: str, content: str, blocked: bool = False) -> None:
        who = "attacker" if role == "attacker" else "target"
        note = " (blocked by the provider's filter)" if blocked else ""
        self.state.emit(
            "status",
            kind="turn",
            message=f"{self.attack.id} run {self.run_index + 1}, turn {self.turn_no}: {who}{note}",
            attack_id=self.attack.id,
            category=self.attack.category.value,
            run=self.run_index,
            turn=self.turn_no,
            role=who,
            text=content[:PREVIEW_CHARS],
            blocked=blocked,
        )

    async def on_agent_end(self, context: Any, agent: Any, output: Any) -> None:
        if isinstance(output, str) and output.strip():
            self.turn("attacker", output.strip())


def format_conversation(turns: list[Turn]) -> str:
    return "\n".join(f"{'YOU' if t.role == 'attacker' else 'TARGET'}: {t.content}" for t in turns)


def _attacker_agent(attack: Attack) -> Agent[ScanState]:
    brief = (f"\n\n# This attack\n\n- Category: {attack.category.value}\n- Goal: {attack.goal}\n"
             f"- Strategy if it resists: {attack.strategy or 'try a new pretext each turn'}\n")
    return Agent[ScanState](
        name="attacker",
        instructions=(config.PROMPTS_DIR / "attacker.md").read_text() + brief,
        model=models.model(config.ATTACK_MODEL),
        model_settings=models.settings_for(config.ATTACK_MODEL),
    )


async def _next_message(state: ScanState, attack: Attack, turns: list[Turn], hooks: AttackProgressHooks) -> str | None:
    """The attacker's next message, or None if the attacker model was blocked or refused."""
    if config.FAKE_LLM:
        await models.fake_delay()
        message = f"I really need this, please. {attack.opening_message}"
        hooks.turn("attacker", message)
        return message

    prompt = (f"Conversation so far:\n{format_conversation(turns)}\n\n"
              f"Write your message for turn {hooks.turn_no} of {config.MAX_TURNS}.")
    run = await models.run_agent(_attacker_agent(attack), prompt, context=state, max_turns=1, hooks=hooks)
    state.add_usage({"input": run.input_tokens, "output": run.output_tokens})
    if run.blocked or run.refusal or not isinstance(run.output, str) or not run.output.strip():
        why = "blocked by the provider's filter" if run.blocked else "refused"
        state.emit("status", message=f"{attack.id} run {hooks.run_index + 1}: the attacker model {why}; ending this run")
        return None
    return run.output.strip()


async def _run_once(state: ScanState, attack: Attack, run_index: int) -> Transcript:
    transcript = Transcript(attack_id=attack.id, run_index=run_index)
    hooks = AttackProgressHooks(state, attack, run_index)
    try:
        for n in range(1, config.MAX_TURNS + 1):
            if state.out_of_time():
                if not transcript.turns:
                    transcript.error = "Skipped: the scan's time budget ran out"
                break
            hooks.turn_no = n
            if n == 1:
                message = attack.opening_message
                hooks.turn("attacker", message)
            else:
                message = await _next_message(state, attack, transcript.turns, hooks)
                if message is None:
                    break

            reply = await adapter.send(state.target, list(transcript.turns), message)
            transcript.turns += [Turn(role="attacker", content=message), reply]
            hooks.turn("target", reply.content, blocked=reply.blocked)

            if reply.blocked:
                transcript.provider_blocked = True
                break
            if find_canary(state.target, reply.content):
                break  # leak proven; more turns add nothing
    except NotBuiltYet:
        raise  # let the orchestrator report the missing step
    except Exception as exc:  # noqa: BLE001  one broken run must not sink the others
        transcript.error = f"{type(exc).__name__}: {exc}"
    return transcript


@observe(name="fanout")
async def run_all(state: ScanState, attacks: list[Attack], k: int) -> dict[str, list[Transcript]]:
    runs = await asyncio.gather(*(_run_once(state, a, i) for a in attacks for i in range(k)))
    out: dict[str, list[Transcript]] = {a.id: [] for a in attacks}
    for t in runs:
        out[t.attack_id].append(t)
    return out
