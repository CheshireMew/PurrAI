"""Independent coverage, sample management, and exact evidence before side effects."""
import asyncio
import json
import time

import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.advertisement_schemas import AdvertisementImport, AdvertisementSampleInput, AdvertisementSettings
from purrai.advertisement_similarity import compare
from purrai.management import Management
from purrai.models import Action, AdvertisementReview, AdvertisementSample, Group, Meta, Violation


class ReviewModel:
    def __init__(self, decide=None):
        self.calls = []
        self.decide = decide or (lambda row: True)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.wait = False
        self.omit = set()

    async def completion(self, system, payload, **kwargs):
        rows = json.loads(payload)["messages"]
        self.calls.append(rows)
        self.entered.set()
        if self.wait:
            await self.release.wait()
        return {"decisions": [{"id": row["id"], "advertisement": self.decide(row), "scope": "text",
                               "reason": "独立审核正文"} for row in rows if row["text"] not in self.omit]}, 100

    async def close(self):
        pass


async def reviewer(env, decide=None):
    settings, db, tg, service = env
    settings.chat_api_key = "offline-key"
    model = ReviewModel(decide)
    service.advertisement_review.model = model
    return service.advertisement_review, model


async def records(db, model):
    async with db.sessions() as session:
        return (await session.scalars(select(model))).all()


async def add_sample(env, text, **changes):
    settings, db, tg, _ = env
    admin = Management(db, tg, settings=settings).advertisements
    return await admin.save(1, -1001, AdvertisementSampleInput(text=text, reason="人工独立确认", **changes), "sample-" + str(time.time_ns()))


async def test_corrected_verdict_cannot_contribute_to_later_mute(env):
    from purrai.ai_advertising import advertisement_count
    from purrai.schemas import ViolationCorrectionInput
    settings, db, tg, service = env
    worker, _ = await reviewer(env)
    for i, body in enumerate(("春天院子里的树木开花了", "船只清晨准时靠岸", "同学准备出门上课", "明天去山上看日出"), 1):
        await service.message(message(i, body))
        await worker.run_one()
        if i == 1:
            violation, = await records(db, Violation)
            await Management(db, tg, settings=settings).corrections.apply(1, -1001, violation.id,
                ViolationCorrectionInput(reason="已独立核实误判", request_id="correct-first-verdict"))
            sample, = await records(db, AdvertisementSample)
            assert not sample.enabled
        await drain(service)
        async with db.sessions() as session:
            assert await advertisement_count(session, -1001, 42) == i - 1
        assert any(name == "restrict_chat_member" for name, _ in tg.calls) == (i == 4)


async def test_older_archive_does_not_cancel_later_edit(env):
    worker, model = await reviewer(env, lambda row: False)
    _, db, _, service = env
    original = message(35, "归档原文")
    edit = {**original, "text": "编辑后文字", "edit_date": original["date"] + 1}
    await service.message(original, update_id=4)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": False}
    await service.message(edit, edited=True, update_id=5)
    assert await worker.run_one()
    assert [row["text"] for batch in model.calls for row in batch] == ["编辑后文字"]
    assert {row.state for row in await records(db, AdvertisementReview)} == {"superseded", "clean"}


async def test_correction_invalidates_a_pending_cumulative_mute(env):
    from purrai.schemas import ViolationCorrectionInput
    settings, db, tg, service = env
    worker, _ = await reviewer(env)
    for i, body in enumerate(("森林边有几株桂花", "远洋客轮今天进港", "大家明早去教室"), 1):
        await service.message(message(i, body))
        await worker.run_one()
    mute, = [row for row in await records(db, Action) if row.kind == "ai_ad_mute"]
    assert mute.state == "pending"
    first = next(row for row in await records(db, Violation) if row.message_id == 1)
    await Management(db, tg, settings=settings).corrections.apply(1, -1001, first.id,
        ViolationCorrectionInput(reason="复核了第一条消息", request_id="correct-before-mute"))
    await drain(service)
    async with db.sessions() as session:
        assert (await session.get(Action, mute.id)).state == "cancelled"
    assert not any(name == "restrict_chat_member" for name, _ in tg.calls)


async def test_a_busy_group_yields_the_next_batch_to_a_waiting_group(env):
    _, db, _, service = env
    worker, _ = await reviewer(env, lambda row: False)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        session.add(Group(id=-2000, approved=True, enabled=True, settings=group.settings))
    def busy(i):
        return message(i, "另一群的消息", chat={"type": "supergroup", "id": -2000})
    await service.message(busy(1))
    assert await worker.run_one()
    await service.message(busy(2))
    await service.message(message(1, "等待审核"))
    assert await worker.run_one()
    states = {(r.group_id, r.message_id): r.state for r in await records(db, AdvertisementReview)}
    assert states[-1001, 1] == "clean" and states[-2000, 2] == "pending"


async def test_offline_rescan_reaches_messages_beyond_the_first_page(env):
    _, db, _, service = env
    worker = service.advertisement_review
    original = "晚加入样本库的可替换长文本用于独立验证"
    async with db.transaction() as session:
        from purrai.advertisement_store import revision
        for i in range(26):
            msg = message(i + 1, original if i == 25 else f"普通对话{i}")
            session.add(AdvertisementReview(group_id=-1001, user_id=42, message_id=i + 1,
                message=msg, revision=revision(msg), event_at=msg["date"], created_at=msg["date"] + i))
    assert await worker.run_one()
    assert await worker.run_one()
    assert not await worker.run_one()
    await add_sample(env, original)
    assert await worker.run_one()
    assert await worker.run_one()
    assert not await worker.run_one()
    states = {row.message_id: row.state for row in await records(db, AdvertisementReview)}
    assert states[26] == "matched" and states[1] == "pending"


def test_shortened_real_template_and_numeric_variants():
    old = "上什么逼班，来我这一个月直接提奔驰，2026年想翻身找我就对了"
    new = "上\u200b什么逼班\ufeff，来我这一个月直接\u200c提奔驰"
    assert compare(new, old)["direct"]
    assert compare(new, old)["score"] >= 0.9
    assert compare("参加虚构星光工作每日5000立即联系我", "参加虚构星光工作每日3000立即联系我")["direct"]
    assert not compare("有人发“" + new + "”，这是诈骗，大家别信", old)["direct"]
    assert not compare("这是诈骗大家别信" + new, old)["direct"]
    assert not compare("来我这", old)["direct"]


@pytest.mark.parametrize("amount", ["丨ooo", "I000", "1Ｏ〇0", "1️⃣0️⃣0️⃣0️⃣", "❶⓿⓿⓿", "2o2S", "Z000", "B000"])
def test_lookalike_and_emoji_numbers_are_same_template_variable(amount):
    template = "参加虚构星光工作可获{}积分立即联系负责人"
    result = compare(template.format(amount), template.format("1000"))
    assert result["direct"] and result["score"] == 1.0
    assert not compare("请勿相信" + template.format(amount), template.format("1000"))["direct"]


def test_ordinary_letters_and_short_codes_cannot_replace_sample_numbers():
    template = "参与团队项目的工作人员编号是{}请查看详细说明"
    for value in ("o2", "iooo", "v1ooo"):
        assert not compare(template.format(value), template.format("1000"))["direct"]


async def test_audit_runs_with_chat_disabled_and_ninety_second_cooldown(env):
    _, db, _, service = env
    worker, model = await reviewer(env)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": False, "chat_interval": 90}
        session.add(Meta(key="chat_attention:-1001", value=json.dumps({"last_attempt": time.time()})))
    await service.message(message(1, "本次测试一个全新推广内容请加入虚构项目"))
    assert await worker.run_one()
    assert len(model.calls) == 1
    assert (await records(db, AdvertisementReview))[0].state == "advertisement"
    await drain(service)
    assert any(method == "delete_message" and args["message_id"] == 1 for method, args in env[2].calls)


async def test_busy_requests_batch_all_members_without_losing_any_verdict(env):
    _, db, _, service = env
    worker, model = await reviewer(env, lambda row: "推广" in row["text"])
    model.wait = True
    await service.message(message(1, "第一条是正常闲聊消息"))
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.entered.wait(), 3)
    await service.message(message(2, "第二条是虚构项目推广消息", user_id=43))
    await service.message(message(3, "第三条是正常聊天消息", user_id=44))
    model.release.set()
    await task
    assert await worker.run_one()
    assert [len(rows) for rows in model.calls] == [1, 2]
    states = {row.message_id: row.state for row in await records(db, AdvertisementReview)}
    assert states == {1: "clean", 2: "advertisement", 3: "clean"}


async def test_same_template_other_account_deletes_locally_without_learning_from_match(env):
    _, db, tg, service = env
    worker, model = await reviewer(env)
    text = "这里是测试招揽话术请来参加虚构星光项目领取2000点"
    await service.message(message(1, text))
    await worker.run_one()
    await service.message(message(2, text.replace("2000", "Z0️⃣o〇"), user_id=43))
    assert len(model.calls) == 1
    assert len(await records(db, AdvertisementSample)) == 1
    states = {row.message_id: row.state for row in await records(db, AdvertisementReview)}
    assert states == {1: "advertisement", 2: "matched"}
    await drain(service)
    assert any(method == "delete_message" and args["message_id"] == 2 for method, args in tg.calls)


async def test_model_omission_remains_pending_and_only_missing_message_retried(env):
    _, db, _, service = env
    worker, model = await reviewer(env, lambda _: False)
    model.omit = {"此消息模型先漏答"}
    await service.message(message(1, "此消息模型正常回答"))
    await service.message(message(2, "此消息模型先漏答", user_id=43))
    await worker.run_one()
    states = {row.message_id: row.state for row in await records(db, AdvertisementReview)}
    assert states == {1: "clean", 2: "pending"}
    async with db.transaction() as session:
        row = await session.scalar(select(AdvertisementReview).where(AdvertisementReview.message_id == 2))
        row.available_at = 0
    model.omit.clear()
    await worker.run_one()
    assert len(model.calls[-1]) == 1
    assert all(row.state == "clean" for row in await records(db, AdvertisementReview))


async def test_configuration_change_while_ai_busy_invalidates_old_result(env):
    settings, db, tg, service = env
    worker, model = await reviewer(env)
    model.wait = True
    await service.message(message(1, "新消息需要独立判断是否属于广告"))
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.entered.wait(), 3)
    admin = Management(db, tg, settings=settings).advertisements
    await admin.configure(1, -1001, AdvertisementSettings(threshold=0.95), 0, "change-while-busy")
    model.release.set()
    await task
    assert (await records(db, AdvertisementReview))[0].state == "pending"
    assert not await records(db, Violation)


async def test_edit_while_ai_busy_does_not_delete_or_learn_old_content(env):
    _, db, tg, service = env
    worker, model = await reviewer(env)
    model.wait = True
    original = message(1, "待审核的原始测试内容")
    await service.message(original, update_id=1)
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.entered.wait(), 3)
    await service.message({**original, "text": "已经修改成正常消息", "edit_date": original["date"] + 1}, True, update_id=2)
    model.release.set()
    await task
    assert not await records(db, AdvertisementSample)
    assert not any(method == "delete_message" for method, _ in tg.calls)
    assert sorted(row.state for row in await records(db, AdvertisementReview)) == ["pending", "superseded"]


async def test_sample_disabled_before_delete_cancels_action(env):
    _, db, tg, service = env
    text = "测试模板内容足够长而且是独立确认的招揽文本"
    saved = await add_sample(env, text)
    await service.message(message(1, text))
    async with db.transaction() as session:
        row = await session.get(AdvertisementSample, saved["id"])
        row.enabled = False
        row.version += 1
    await drain(service)
    assert not any(method == "delete_message" for method, _ in tg.calls)
    deletes = [row for row in await records(db, Action) if row.kind == "delete"]
    assert deletes and all(row.state == "cancelled" for row in deletes)


async def test_corrected_sample_cannot_match_and_queued_action_is_cancelled(env):
    _, db, tg, service = env
    text = "这是一条用于测试纠错关联的虚构广告样本文本"
    saved = await add_sample(env, text)
    async with db.transaction() as session:
        sample = await session.get(AdvertisementSample, saved["id"])
        sample.violation_id = "original-evidence"
    await service.message(message(1, text))
    async with db.transaction() as session:
        session.add(Meta(key="violation-correction:original-evidence", value="{}"))
    await drain(service)
    await service.message(message(2, text, user_id=43))
    assert not any(method == "delete_message" for method, _ in tg.calls)
    assert next(row for row in await records(db, AdvertisementReview) if row.message_id == 2).state == "pending"


@pytest.mark.parametrize("scope,reference", [("context", False), ("text", True)])
async def test_context_dependent_samples_or_quoted_messages_require_ai(env, scope, reference):
    _, db, _, service = env
    text = "这是用来测试引用上下文保护的虚构广告样本文本"
    await add_sample(env, text, scope=scope)
    kwargs = {"reply_to_message": {"message_id": 50, "text": "这是举报", "from": {"id": 10}}} if reference else {}
    await service.message(message(1, text, **kwargs))
    assert (await records(db, AdvertisementReview))[0].state == "pending"


async def test_quota_exhaustion_keeps_review_visible_for_later(env):
    settings, db, _, service = env
    worker, model = await reviewer(env)
    settings.chat_daily_tokens = 1000
    await service.message(message(1, "需要等待额度的测试消息"))
    await worker.run_one()
    row = (await records(db, AdvertisementReview))[0]
    assert row.state == "pending" and row.error and row.available_at > time.time()
    assert not model.calls and not await records(db, Violation)


async def test_http_save_edit_disable_arbitrary_sample_and_version_conflict(client):
    path = "/api/groups/-1001/advertisement-samples"
    first = "任意替换样本甲的完整正文用于管理入口验证"
    second = "任意替换样本乙完全不同的文字也能动态管理"
    data = {"text": first, "reason": "人工独立确认", "request_id": "frontend-request-id"}
    saved = await client.post(path, json=data, headers={"Idempotency-Key": "create-sample-01"})
    assert saved.status_code == 200, saved.text
    row = saved.json()
    hit = await client.post(path + "/preview", json={"text": first})
    assert hit.json()["action"] == "delete"
    updated = await client.put(path + "/" + row["id"], json={**data, "text": second},
        headers={"Idempotency-Key": "update-sample-01", "If-Match": str(row["version"])})
    assert updated.status_code == 200, updated.text
    row2 = updated.json()
    assert (await client.post(path + "/preview", json={"text": first})).json()["action"] == "review"
    assert (await client.post(path + "/preview", json={"text": second})).json()["action"] == "delete"
    stale = await client.put(path + "/" + row["id"], json=data,
        headers={"Idempotency-Key": "stale-sample-001", "If-Match": str(row["version"])})
    assert stale.status_code == 409
    disabled = await client.put(path + "/" + row["id"], json={**data, "text": second, "enabled": False},
        headers={"Idempotency-Key": "disable-sample-1", "If-Match": str(row2["version"])})
    assert disabled.status_code == 200
    assert (await client.post(path + "/preview", json={"text": second})).json()["action"] == "review"


async def test_history_import_is_idempotent_and_not_automatic_text_proof(env):
    settings, db, tg, _ = env
    async with db.transaction() as session:
        session.add(Violation(group_id=-1001, user_id=42, message_id=1,
                              excerpt="来自历史模型确认的虚构广告消息", reasons=["AI确认广告，删除消息"]))
        session.add(Violation(group_id=-1001, user_id=43, message_id=2,
                              excerpt="普通新人链接违规不能变成广告样本", reasons=["新成员限制期发送链接"]))
    admin = Management(db, tg, settings=settings).advertisements
    preview = await admin.import_history(1, -1001, AdvertisementImport())
    assert preview["count"] == 1 and not await records(db, AdvertisementSample)
    saved = await admin.import_history(1, -1001, AdvertisementImport(apply=True), "history-import-01")
    assert saved["count"] == 1
    assert (await records(db, AdvertisementSample))[0].scope == "context"
    again = await admin.import_history(1, -1001, AdvertisementImport(apply=True), "history-import-02")
    assert again["count"] == 0


async def test_imported_history_is_independently_rechecked_without_punishment(env):
    settings, db, tg, service = env
    worker, model = await reviewer(env)
    text = "历史已确认的虚构推广模板新增额度B000点"
    async with db.transaction() as session:
        session.add(Violation(group_id=-1001, user_id=42, message_id=50,
                              excerpt=text, reasons=["AI确认广告，删除消息"]))
    admin = Management(db, tg, settings=settings).advertisements
    await admin.import_history(1, -1001, AdvertisementImport(apply=True), "history-recheck-01")
    assert await worker.run_one()
    assert model.calls[0][0]["number_readings"] == {"text": {"B000": "8000"}}
    saved = (await records(db, AdvertisementSample))[0]
    assert saved.scope == "text" and saved.source == "model" and saved.review_state == "done"
    assert all(row.kind == "advertisement_import" for row in await records(db, Action))
    await service.message(message(51, text, user_id=43))
    assert (await records(db, AdvertisementReview))[0].state == "matched"
    assert len(model.calls) == 1


async def test_chat_waits_for_own_verdict_and_does_not_duplicate_adjudication(env):
    from test_chat import configured
    from purrai.chat_model import ChatReply
    chat, chatter = await configured(env, chat_proactive=False)
    worker, model = await reviewer(env, lambda row: False)
    incoming = message(1, "柴郡喵，周末去哪里玩比较合适？")
    await env[3].message(incoming)
    await chat.observe(incoming)
    assert not await chat.run_one() and not chatter.calls
    assert await worker.run_one()
    # Even if the conversational model violates the prompt, it cannot invent a second verdict.
    chatter.result = ChatReply(should_reply=False, is_advertisement=True)
    assert await chat.run_one()
    assert len(model.calls) == len(chatter.calls) == 1
    assert chatter.calls[0][1]["messages"][0]["advertisement_reviewed"] is True
    assert "keyword_hints" not in chatter.calls[0][1]
    assert "本次只判断是否接话" in chatter.calls[0][0]
    assert not await records(env[1], Violation)


async def test_waiting_message_not_merged_or_included_in_clean_chat_context(env):
    from test_chat import configured
    from purrai.models import ChatTurn
    chat, chatter = await configured(env, chat_proactive=False)
    worker, model = await reviewer(env, lambda row: False)
    first = message(1, "柴郡喵，给我推荐一种周末活动", user_id=42)
    await env[3].message(first)
    await chat.observe(first)
    await worker.run_one()
    second = message(2, "柴郡喵，这个后来的独立问题仍然等待审核", user_id=43)
    await env[3].message(second)
    await chat.observe(second)
    assert await chat.run_one()
    assert [row["id"] for row in chatter.calls[0][1]["messages"]] == [1]
    waiting = next(row for row in await records(env[1], ChatTurn) if row.message_id == 2)
    assert waiting.state == "pending" and waiting.attempts == 0


async def test_local_matching_works_without_model_and_during_busy_request(env):
    _, db, tg, service = env
    worker, model = await reviewer(env, lambda row: False)
    sample = "这是一个已经独立确认且可跨账号使用的重复广告样本"
    await add_sample(env, sample)
    model.wait = True
    await service.message(message(1, "另一条未知内容需要模型审核"))
    task = asyncio.create_task(worker.run_one())
    await asyncio.wait_for(model.entered.wait(), 3)
    try:
        await service.message(message(2, sample, user_id=43))
        await drain(service)
        assert any(method == "delete_message" and args["message_id"] == 2 for method, args in tg.calls)
    finally:
        model.release.set()
        await task
    env[0].chat_api_key = ""
    await service.message(message(3, sample, user_id=44))
    assert next(row for row in await records(db, AdvertisementReview) if row.message_id == 3).state == "matched"
    assert len(model.calls) == 1


async def test_empty_edit_invalidates_old_queued_action_and_configured_commands_skip_ai(env, client):
    _, db, tg, service = env
    sample = "这是一条用于验证消息清空后旧删除动作失效的完整样本"
    await add_sample(env, sample)
    from test_recognition_policy import save
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"]["utility_patterns"].append({"id": "test-help", "title": "帮助口令", "enabled": True, "pattern": "/help"})
    await save(client, document)
    original = message(1, sample)
    await service.message(original, update_id=1)
    await service.message({**original, "text": "", "edit_date": original["date"] + 1}, True, update_id=2)
    await service.message(message(2, "/help"))
    await drain(service)
    assert not any(method == "delete_message" for method, _ in tg.calls)
    assert sorted(row.state for row in await records(db, AdvertisementReview)) == ["ignored", "ignored", "superseded"]


async def test_context_verdict_never_becomes_reusable_text_even_if_model_says_text(env):
    _, db, _, service = env
    worker, _ = await reviewer(env)
    await service.message(message(1, "这段正文的含义必须联系被引用的消息才能判断", reply_to_message={"message_id": 80, "text": "引用背景"}))
    await worker.run_one()
    assert (await records(db, AdvertisementSample))[0].scope == "context"


async def test_restart_recovers_requesting_review(env):
    _, db, _, service = env
    worker, model = await reviewer(env, lambda row: False)
    await service.message(message(1, "重启前尚未完成审核的消息"))
    async with db.transaction() as session:
        row = await session.scalar(select(AdvertisementReview))
        row.state = "requesting"
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(model.entered.wait(), 3)
        for _ in range(100):
            if (await records(db, AdvertisementReview))[0].state == "clean":
                break
            await asyncio.sleep(0.01)
        assert (await records(db, AdvertisementReview))[0].state == "clean"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_optional_sample_mute_uses_configured_action(env):
    settings, db, tg, service = env
    text = "用于验证可配置永久禁言行为的独立广告样本内容"
    await add_sample(env, text)
    admin = Management(db, tg, settings=settings).advertisements
    await admin.configure(1, -1001, AdvertisementSettings(sample_action="permanent_mute"), 0, "sample-mute-01")
    await service.message(message(1, text))
    await drain(service)
    assert any(method == "restrict_chat_member" and args["until_date"] == 0 for method, args in tg.calls)
    assert any(method == "delete_message" and args["message_id"] == 1 for method, args in tg.calls)
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    assert len(await records(db, AdvertisementSample)) == 1


@pytest.mark.parametrize("change", ["unknown", "duplicate", "invalid"])
@pytest.mark.parametrize("history", [False, True])
async def test_invalid_batch_result_never_silently_passes_a_message(env, change, history):
    worker, model = await reviewer(env)
    if history:
        async with env[1].transaction() as session:
            session.add(AdvertisementSample(group_id=-1001, text="任意待复核的历史文本", scope="context",
                                           source="history", source_key="invalid-history", review_state="pending"))
    else:
        await env[3].message(message(1, "本条消息测试无效模型响应的恢复行为"))
    original = model.completion
    async def malformed(*args, **kwargs):
        result, tokens = await original(*args, **kwargs)
        decision = result["decisions"][0]
        if change == "unknown":
            decision["id"] = "unknown"
        elif change == "duplicate":
            result["decisions"].append(decision.copy())
        else:
            decision["advertisement"] = "true"
        return result, tokens
    model.completion = malformed
    await worker.run_one()
    if history:
        assert (await records(env[1], AdvertisementSample))[0].review_state == "pending"
        assert not await records(env[1], Action)
    else:
        assert (await records(env[1], AdvertisementReview))[0].state == "pending"
    assert not await records(env[1], Violation)


async def test_advertisement_decision_with_paraphrased_reason_applies_in_one_request(env):
    worker, model = await reviewer(env, lambda item: item["text"].startswith("青岚绘图器"))
    original = model.completion

    async def explained_result(*args, **kwargs):
        result, tokens = await original(*args, **kwargs)
        for decision in result["decisions"]:
            if decision["advertisement"]:
                decision["reason"] = "发布者正在为其数据可视化软件介绍功能与适用平台"
        return result, tokens

    model.completion = explained_result
    await env[3].message(message(641, "青岚绘图器：自动分析数据并生成精美图表，桌面与手机版均支持"))
    await env[3].message(message(642, "谢谢大家，我已经修好了问题", user_id=43))
    assert await worker.run_one()
    reviews = {row.message_id: row for row in await records(env[1], AdvertisementReview)}
    assert reviews[641].state == "advertisement" and reviews[642].state == "clean"
    assert reviews[641].result["reason"] == "发布者正在为其数据可视化软件介绍功能与适用平台"
    assert len(model.calls) == 1
    await drain(env[3])
    assert {data["message_id"] for method, data in env[2].calls if method == "delete_message"} == {641}


async def test_historical_sample_accepts_advertisement_decision_without_a_quote(env):
    worker, model = await reviewer(env)
    async with env[1].transaction() as session:
        session.add(AdvertisementSample(group_id=-1001, text="这是一个等待重新核实的历史推广样本正文",
            source="import", source_key="history-invalid-quote", scope="context", review_state="pending"))
    assert await worker.run_one()
    sample, = await records(env[1], AdvertisementSample)
    assert sample.enabled and sample.review_state == "done" and sample.scope == "text"
    assert not await records(env[1], Violation)


def test_negated_replacement_is_not_a_direct_match_despite_high_score():
    sample = "这是一个虚构的广告示例大家快来参加报名领取奖品"
    changed = "这是一个虚构的广告示例大家别来参加报名领取奖品"
    result = compare(changed, sample)
    assert result["score"] > 0.9 and not result["direct"]


def test_samples_migration_keeps_existing_data(tmp_path):
    import os
    from pathlib import Path
    import sqlite3
    import subprocess
    import sys
    path = tmp_path / "sample-migration.sqlite3"
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{path}"}
    for target in ("0031", "0032"):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        with sqlite3.connect(path) as connection:
            if target == "0031":
                connection.execute("INSERT INTO meta (key,value) VALUES ('advertisement-migration-check','preserved')")
            else:
                assert connection.execute("SELECT value FROM meta WHERE key='advertisement-migration-check'").fetchone() == ("preserved",)
                for name in ("advertisement_policies", "advertisement_samples", "advertisement_reviews"):
                    assert connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone() == (0,)
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


async def test_member_rejoin_cancels_old_sample_mute(env):
    from purrai.models import Member
    settings, db, tg, service = env
    text = "用于验证退出重进后旧样本禁言不能跨越成员周期的测试内容"
    await add_sample(env, text)
    admin = Management(db, tg, settings=settings).advertisements
    await admin.configure(1, -1001, AdvertisementSettings(sample_action="permanent_mute"), 0, "sample-rejoin-01")
    await service.message(message(1, text))
    async with db.transaction() as session:
        member = await session.get(Member, (-1001, 42))
        member.generation = "new-join"
    await drain(service)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


async def test_migrated_historical_message_cannot_be_punished_in_new_group(env):
    from purrai.advertisement_store import current
    _, db, _, service = env
    await service.message(message(1, "群升级前尚未完成的历史消息"))
    async with db.transaction() as session:
        row = await session.scalar(select(AdvertisementReview))
        row.message_id = -row.message_id
        assert not await current(session, row)


async def test_review_http_invalid_json_is_retryable_without_changing_chat_contract():
    import httpx
    from purrai.chat_model import ChatModel, ChatModelError
    from purrai.config import Settings
    settings = Settings(_env_file=None, chat_api_base="https://model.test/v1", chat_api_key="offline", chat_model="test")
    model = ChatModel(settings, httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "invalid"}}]})))
    try:
        with pytest.raises(ChatModelError) as error:
            await model.completion("review", "payload", retry_invalid=True)
        assert error.value.retryable
    finally:
        await model.close()
