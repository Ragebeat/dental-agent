"""LINE-канал на живой БД: настоящий MCP-сервер за шлюзом сессий; идентификация приходит с channel='line' и ключом пользователя."""
import asyncio

from dental.channels.eval_bridge import USER, LineAgent
from dental.eval.runner import RunSettings
from graph_helpers import llm_json
from helpers import NOW

ID = "診察券番号は100001、生年月日は1988年4月12日です。"


def test_identity_check_through_line_gateway_is_recorded_with_channel_and_pseudonymous_key(core):
    async def main():
        agent = LineAgent(core, llm_json(), NOW, RunSettings(agent="graph", channel="line"))
        try:
            r1 = await agent.reply("クリーニングの予約をお願いしたいです")
            r2 = await agent.reply(ID)
            key = agent.gw.user_key(USER)
        finally:
            await agent.aclose()
        return r1, r2, key
    r1, r2, key = asyncio.run(main())
    assert "AIの予約アシスタント" in r1 and "ご希望の日にち" in r2
    with core._connect() as conn:
        row = conn.execute("SELECT channel, client_key, success FROM auth_attempts WHERE card_number = 100001"
                           " ORDER BY id DESC LIMIT 1").fetchone()
    assert row["channel"] == "line" and row["success"] and row["client_key"] == f"line:{key}" and USER not in row["client_key"]
