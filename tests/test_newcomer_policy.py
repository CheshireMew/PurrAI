import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Group, Member, Violation, Whitelist, default_settings, now
from purrai.rules import assess
from purrai.wallet_addresses import contains_wallet_address


ADDRESSES = [
    "0x52908400098527886E0F7030069857D2E4169EE7",
    "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
    "bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y",
    "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeL",
    "So11111111111111111111111111111111111111112",
    "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c",
    "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
    "0:" + "0" * 64,
]


@pytest.mark.parametrize("address", ADDRESSES)
def test_wallet_formats_in_text_and_captions(address):
    assert contains_wallet_address(f"收款地址：{address}。")
    assert "新成员观察期发送加密货币地址" in assess(
        address, [], [], default_settings(), newcomer=True,
    )["reasons"]
    assert not assess(address, [], [], default_settings())["reasons"]


@pytest.mark.parametrize("text", [
    "普通文字、订单号 1234567890123456789012345678901234567890",
    "0x" + "a" * 64,  # transaction hash, not an EVM address
    "a" * 64, "1BoatSLRHtKNngkdXEeobR76b53LETtpyU",
    "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wli",
    "TNPeeaaFB7K9cmo4uQpcU32zGK8G1NYqeM",
    "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9d",
    "So111111111111111111111111111111111111111120",
    "前缀0x52908400098527886E0F7030069857D2E4169EE7xyz",
])
def test_wallet_false_positives_and_invalid_checksums(text):
    assert not contains_wallet_address(text)


@pytest.mark.parametrize("age,observed,blocked", [
    (2 * 86400, 10, True), (3 * 86400 + 1, 10, False),
    (4 * 86400, 0, True),  # first three messages still observed
])
async def test_three_day_observation_covers_all_requested_content(env, age, observed, blocked):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now() - age,
                           passed_at=now() - age, observed_messages=observed))
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_enabled": False, "burst_enabled": False,
                          "external_group_ban": False}  # Exercise observation limits independently of channel ads.
    messages = [
        message(1, "频道内容", forward_origin={"type": "channel", "date": 1,
                "chat": {"id": -2001, "type": "channel"}, "message_id": 1}),
        message(2, "用户内容", forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "A"}),
        message(3, "@-1002274900898 @public_group"),
    ]
    if observed:
        messages.extend([
            message(4, "", caption=ADDRESSES[0], document={"file_id": "doc"}),
            message(5, "普通显示名", entities=[{"type": "text_mention", "offset": 0,
                    "length": 5, "user": {"id": 500}}]),
            message(6, "旧版转发", forward_from={"id": 88}),
        ])
    for incoming in messages:
        await svc.message(incoming)
    await drain(svc)
    deleted = [args["message_id"] for name, args in tg.calls if name == "delete_message"]
    assert sorted(deleted) == ([item["message_id"] for item in messages] if blocked else [])
    assert not any(name in ("ban_chat_member", "restrict_chat_member") for name, _ in tg.calls)
    async with db.sessions() as session:
        assert all(not row.warning_active for row in (await session.scalars(select(Violation))).all())


@pytest.mark.parametrize("exempt", ["administrator", "whitelist", "unknown", "disabled"])
async def test_new_observation_exemptions_and_switches(env, exempt):
    _, db, tg, svc = env
    async with db.transaction() as session:
        if exempt != "unknown":
            session.add(Member(group_id=-1001, user_id=42, passed_at=now(), joined_at=now()))
        if exempt == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exempt == "disabled":
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "newcomer_forwards": False, "newcomer_wallets": False}
    if exempt == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await svc.message(message(1, ADDRESSES[0], forward_origin={"type": "user", "date": 1, "sender_user": {"id": 90}}))
    await drain(svc)
    assert not any(name in ("delete_message", "ban_chat_member") for name, _ in tg.calls)
